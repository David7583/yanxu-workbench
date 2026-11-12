"""Read-only archive discovery and resumable local import jobs."""
from __future__ import annotations
import hashlib,json,os,re,threading,zipfile
from pathlib import Path,PurePosixPath
from fastapi import APIRouter,HTTPException
from pydantic import BaseModel,Field
from . import core,importer
from .core import conn,now,uid,json_load
router=APIRouter(prefix='/api')

def init_schema():
    with conn() as db:
        db.execute('CREATE TABLE IF NOT EXISTS archive_scans(id TEXT PRIMARY KEY,created_at TEXT NOT NULL,payload TEXT NOT NULL)')
        cols={r[1] for r in db.execute('PRAGMA table_info(imports)')}
        for name,definition in {'source_path':'TEXT','scan_snapshot':'TEXT','checkpoint':'INTEGER NOT NULL DEFAULT 0'}.items():
            if name not in cols:db.execute(f'ALTER TABLE imports ADD COLUMN {name} {definition}')

class ScanBody(BaseModel):
    paths:list[str]=Field(min_length=1,max_length=20)
class ImportBody(BaseModel):
    scan_id:str
    item_ids:list[str]=Field(min_length=1,max_length=500)

def provider_of(path):
    name=str(path).lower()
    for token,label in [('claude','Claude'),('deepseek','DeepSeek'),('grok','Grok'),('千问','千问'),('qwen','千问'),('gemini','Gemini'),('google','Gemini'),('openai','ChatGPT'),('chatgpt','ChatGPT'),('gpt','ChatGPT')]:
        if token in name:return label
    return '自动识别'

def chat_member(name):
    path=PurePosixPath(name.replace('\\','/'))
    if path.is_absolute() or '..' in path.parts:return False
    base=path.name.lower()
    return bool(re.fullmatch(r'conversations(?:[-_][\w-]+)?\.json',base) or base=='prod-grok-backend.json' or base=='myactivity.html' and 'gemini apps' in str(path).lower())

def classify(path):
    name=path.name.lower();provider=provider_of(path)
    if path.suffix.lower()=='.zip':
        try:
            with zipfile.ZipFile(path) as z:
                members=[m for m in z.infolist() if not m.is_dir() and chat_member(m.filename)]
                if len(members)>2000:return None
                if members:return {'kind':'zip','provider':provider,'members':len(members),'expanded_bytes':sum(m.file_size for m in members),'selected':True,'reason':'识别到聊天分片；附件保持原位置，不执行包内程序'}
        except (OSError,zipfile.BadZipFile):return {'kind':'invalid','provider':provider,'selected':False,'reason':'压缩包无法读取'}
        return None
    if path.suffix.lower()=='.json' and (re.match(r'conversations.*\.json$',name) or name=='prod-grok-backend.json' or provider=='千问'):
        return {'kind':'json','provider':provider,'selected':True,'reason':'聊天 JSON'}
    if name=='myactivity.html' and 'gemini apps' in str(path).lower():return {'kind':'html','provider':'Gemini','selected':True,'reason':'Gemini 活动记录；问答完整性待核验'}
    if path.suffix.lower() in ('.md','.markdown','.txt') and len(path.parts)<15:
        return {'kind':'document','provider':provider,'selected':False,'reason':'可选文本资料；可能是附件，不默认导入'}
    if name=='chat.html':return {'kind':'html','provider':provider,'selected':False,'reason':'优先同批 JSON，避免 HTML 重复；超过 64 MB 不可解析'}
    return None

@router.post('/archives/scan')
def scan_archives(body:ScanBody):
    init_schema();items=[];seen=set();files=total_bytes=0;warnings=[]
    for raw in body.paths:
        root=Path(raw).expanduser()
        if not root.is_absolute() or not root.exists():warnings.append(f'路径不存在或不是绝对路径：{raw}');continue
        candidates=[root] if root.is_file() else (Path(d)/n for d,dirs,names in os.walk(root,followlinks=False) for n in names)
        for p in candidates:
            if files>=100000:warnings.append('本次扫描达到 100,000 文件上限，请选择更具体目录');break
            try:
                if p.is_symlink():continue
                full=str(p.resolve())
                if full.casefold() in seen:continue
                seen.add(full.casefold());stat=p.stat();files+=1;total_bytes+=stat.st_size
                classification=classify(p)
                if classification:
                    items.append({'id':uid(),'path':full,'name':p.name,'bytes':stat.st_size,'mtime_ns':stat.st_mtime_ns,**classification})
            except OSError as e:warnings.append(f'无法读取 {p.name}: {e.strerror}')
    # Byte equality is established at import time; same size alone is only a hint.
    sizes={}
    for item in items:
        if item['kind']=='json':
            previous=sizes.get(item['bytes'])
            if previous:item['duplicate_hint']='与另一文件大小相同，导入时计算 SHA-256 核验'
            else:sizes[item['bytes']]=item['id']
    result={'scan_id':uid(),'items':items,'totals':{'files':files,'bytes':total_bytes,'candidates':len(items),'selected':sum(bool(x['selected']) for x in items)},'warnings':warnings[:50]}
    with conn() as db:db.execute('INSERT INTO archive_scans VALUES (?,?,?)',(result['scan_id'],now(),json.dumps(result,ensure_ascii=False)))
    return result

@router.post('/archives/import',status_code=202)
def import_archives(body:ImportBody):
    init_schema()
    with conn() as db:row=db.execute('SELECT payload FROM archive_scans WHERE id=?',(body.scan_id,)).fetchone()
    if not row:raise HTTPException(404,'扫描记录不存在，请重新扫描')
    items={i['id']:i for i in json_load(row['payload'],{}).get('items',[])}
    if any(i not in items for i in body.item_ids):raise HTTPException(422,'所选文件不属于此扫描批次')
    chosen=[items[i] for i in dict.fromkeys(body.item_ids)]
    for item in chosen:
        p=Path(item['path'])
        if not p.is_file() or p.stat().st_size!=item['bytes'] or p.stat().st_mtime_ns!=item['mtime_ns']:raise HTTPException(409,f'文件已变化，请重新扫描：{p.name}')
        if item['kind']=='invalid':raise HTTPException(422,'无法导入损坏的压缩包')
    jobs=[]
    with conn() as db:
        db.execute('BEGIN IMMEDIATE')
        for item in chosen:
            active=db.execute("SELECT id,status FROM imports WHERE source_path=? AND status IN ('queued','processing','cancelling') LIMIT 1",(item['path'],)).fetchone()
            if active:
                jobs.append({'job_id':active['id'],'path':item['path'],'status':active['status'],'already_running':True});continue
            job_id=uid();stamp=now()
            db.execute('INSERT INTO imports(id,filename,status,created_at,updated_at,bytes_total,source_path,scan_snapshot) VALUES (?,?,?,?,?,?,?,?)',(job_id,item['name'],'queued',stamp,stamp,item['bytes'],item['path'],json.dumps(item,ensure_ascii=False)))
            jobs.append({'job_id':job_id,'path':item['path'],'status':'queued'})
    for job in jobs:
        if not job.get('already_running'):
            with importer.JOB_LOCK:importer.CANCELLED.setdefault(job['job_id'],threading.Event())
            importer.POOL.submit(process_archive,job['job_id'])
    return {'jobs':jobs}

def archive_records(path:Path,stop):
    if path.suffix.lower()=='.zip':
        with zipfile.ZipFile(path) as z:
            members=[m for m in z.infolist() if not m.is_dir() and chat_member(m.filename)]
            if not members:raise ValueError('包内没有支持的聊天导出')
            if sum(m.file_size for m in members)>20*1024**3:raise ValueError('聊天分片总量超过 20 GB，请分批导入')
            for member in members:
                if stop.is_set():return
                if member.file_size>2*1024**3 or member.file_size>max(1,member.compress_size)*500:raise ValueError('压缩成员过大或压缩比例异常')
                if member.filename.lower().endswith('.json'):
                    with z.open(member) as f:
                        for record,pos in importer.iter_json_stream(f,member.filename):yield record,str(path)+'::'+member.filename
                else:
                    if member.file_size>64*1024**2:raise ValueError('HTML 超过 64 MB，请改用 JSON')
                    temp=core.DATA_DIR/'imports'/(uid()+'.html');temp.parent.mkdir(parents=True,exist_ok=True)
                    try:
                        with z.open(member) as src,temp.open('xb') as dst:
                            while chunk:=src.read(1024*1024):dst.write(chunk)
                        for record,pos in importer.iter_html(temp,member.filename):yield record,str(path)+'::'+member.filename
                    finally:temp.unlink(missing_ok=True)
    else:
        iterator=importer.iter_json(path,path.name) if path.suffix.lower()=='.json' else importer.iter_html(path,str(path)) if path.suffix.lower() in ('.html','.htm') else importer.iter_markdown(path,path.name)
        for record,pos in iterator:yield record,str(path)

def retained_path(row):
    """Resolve old uploads without requiring source_path, within our retained directory."""
    value=dict(row)
    if value.get('source_path'):
        path=Path(value['source_path'])
        if path.is_file():return path
        # Stored uploads can move with the data directory.
    suffix=Path(value['filename']).suffix.lower()
    if suffix not in ('.json','.html','.htm','.md','.markdown','.txt','.zip'):return None
    folder=(core.DATA_DIR/'imports').resolve()
    path=(folder/(value['id']+suffix)).resolve()
    if path.parent==folder and path.is_file():return path
    return None


def resume_capability(row):
    value=dict(row)
    if value['status'] not in ('cancelled','interrupted','error','failed'):
        return False,'任务正在停止，请稍候' if value['status']=='cancelling' else ''
    with importer.JOB_LOCK:
        if value['id'] in importer.WORKERS:return False,'任务正在收尾，请稍候'
    if str(value.get('error','')).startswith(('原始文件内容已经变化','源文件已变化')):
        return False,'原始文件已变化，请重新扫描或上传'
    try:
        path=retained_path(value)
        if path is None:return False,'原始文件已移动或缺失，请重新选择原始文件导入'
        stat=path.stat()
    except OSError:return False,'暂时无法读取原始文件，请检查文件位置和访问权限'
    snapshot=json_load(value.get('scan_snapshot'),{})
    if snapshot and (stat.st_size!=snapshot.get('bytes') or stat.st_mtime_ns!=snapshot.get('mtime_ns')):
        return False,'原始文件已变化，请重新扫描或上传'
    if not snapshot and value.get('bytes_total') and stat.st_size!=value['bytes_total']:
        return False,'原始文件大小已变化，请重新上传'
    return True,''


def process_archive(job_id):
    with importer.JOB_LOCK:
        if job_id in importer.WORKERS:return
        with conn() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM imports WHERE id=?',(job_id,)).fetchone()
            if not row or row['status'] not in ('queued','cancelling'):
                importer.CANCELLED.pop(job_id,None);return
            row=dict(row)
            stop=importer.CANCELLED.setdefault(job_id,threading.Event())
            if row['status']=='cancelling':stop.set()
            db.execute("UPDATE imports SET status=?,error='',updated_at=? WHERE id=?",('cancelling' if stop.is_set() else 'processing',now(),job_id))
        importer.WORKERS.add(job_id)
    checkpoint=row.get('checkpoint',0);imported=row['imported'];duplicates=row['duplicates'];count=checkpoint;warnings=json_load(row['warnings'],[])
    try:
        path=retained_path(row)
        if path is None:raise ValueError('原始文件已移动或缺失，请重新选择文件导入')
        snapshot=json_load(row['scan_snapshot'],{})
        if snapshot and (path.stat().st_size!=snapshot.get('bytes') or path.stat().st_mtime_ns!=snapshot.get('mtime_ns')):raise ValueError('源文件已变化，请重新扫描或上传')
        hasher=hashlib.sha256()
        with path.open('rb') as f:
            while chunk:=f.read(4*1024*1024):
                if stop.is_set():break
                hasher.update(chunk)
        if stop.is_set():
            with conn() as db:db.execute("UPDATE imports SET status='cancelled',updated_at=? WHERE id=?",(now(),job_id))
            return
        file_hash=hasher.hexdigest()
        if row['file_hash'] and row['file_hash']!=file_hash:raise ValueError('原始文件内容已经变化，不能沿用旧进度；请重新导入')
        with conn() as db:
            same=db.execute("SELECT total FROM imports WHERE file_hash=? AND status='done' AND id<>? LIMIT 1",(file_hash,job_id)).fetchone()
            db.execute('UPDATE imports SET file_hash=? WHERE id=?',(file_hash,job_id))
            if same and not checkpoint:
                db.execute("UPDATE imports SET status='done',progress=100,duplicates=?,total=?,updated_at=? WHERE id=?",(same['total'],same['total'],now(),job_id));return
        for index,(record,origin) in enumerate(archive_records(path,stop)):
            if stop.is_set():break
            if index<checkpoint:continue
            if importer.store_conversation(record,origin,row['filename']):imported+=1
            else:duplicates+=1
            count=index+1;warnings=list(dict.fromkeys(warnings+record['metadata'].get('warnings',[])))[:40]
            with conn() as db:db.execute('UPDATE imports SET total=?,checkpoint=?,imported=?,duplicates=?,warnings=?,updated_at=? WHERE id=?',(count,count,imported,duplicates,json.dumps(warnings,ensure_ascii=False),now(),job_id))
        with conn() as db:db.execute('UPDATE imports SET status=?,progress=?,updated_at=? WHERE id=?',('cancelled' if stop.is_set() else 'done',row['progress'] if stop.is_set() else 100,now(),job_id))
    except Exception as e:
        with conn() as db:db.execute('UPDATE imports SET status=?,error=?,updated_at=? WHERE id=?',('cancelled' if stop.is_set() else 'error','' if stop.is_set() else str(e)[:800],now(),job_id))
    finally:
        with importer.JOB_LOCK:
            importer.WORKERS.discard(job_id)
            if importer.CANCELLED.get(job_id) is stop:importer.CANCELLED.pop(job_id,None)


@router.post('/imports/{job_id}/resume',status_code=202)
def resume_archive(job_id:str):
    with importer.JOB_LOCK:
        with conn() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM imports WHERE id=?',(job_id,)).fetchone()
            if not row:raise HTTPException(404,'导入任务不存在')
            if row['status']=='done':return {'job_id':job_id,'status':'done'}
            possible,reason=resume_capability(row)
            if not possible:raise HTTPException(409,reason or '任务已在队列中或正在运行，请勿重复继续')
            path=retained_path(row);stat=path.stat();legacy=not row['source_path']
            checkpoint=max(row['checkpoint'],row['total']) if legacy else row['checkpoint']
            db.execute("UPDATE imports SET status='queued',error='',source_path=?,scan_snapshot=?,checkpoint=?,updated_at=? WHERE id=?",(str(path),json.dumps({'bytes':stat.st_size,'mtime_ns':stat.st_mtime_ns}),checkpoint,now(),job_id))
        importer.CANCELLED[job_id]=threading.Event()
        try:importer.POOL.submit(process_archive,job_id)
        except RuntimeError:
            importer.CANCELLED.pop(job_id,None)
            with conn() as db:db.execute("UPDATE imports SET status='interrupted',error='任务调度暂不可用，请重启后继续' WHERE id=?",(job_id,))
            raise HTTPException(503,'任务调度暂不可用，请重启后继续')
    return {'job_id':job_id,'status':'queued','checkpoint':checkpoint}
