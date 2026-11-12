"""First-run setup, local folder selection and restart-safe storage relocation."""
from __future__ import annotations
import json,os,subprocess,base64
from pathlib import Path
from fastapi import APIRouter,HTTPException,Query
from pydantic import BaseModel,Field
from . import core
from .core import conn,now,json_load
router=APIRouter(prefix='/api')

def init_schema():pass

def read_setup():
    with conn() as db:
        row=db.execute("SELECT value FROM settings WHERE key='workspace_setup'").fetchone()
        export=db.execute("SELECT value FROM settings WHERE key='export_dir'").fetchone()
    settings=json_load(row['value'],{}) if row else {}
    choices=settings.get('source_dirs',[])
    result={'completed':False,'source_dirs':[],'data_dir':str(core.DATA_DIR),'export_dir':json_load(export['value'],str(core.ROOT/'exports')) if export else str(core.ROOT/'exports'),'language':'both','citation_style':'apa','reduce_motion':False,**settings}
    result['data_dir']=str(core.DATA_DIR);result['suggested_source_dirs']=[p for p in choices if Path(p).is_dir()]
    pending=core.ROOT/'.state'/'storage-request.json'
    try:result['pending_data_dir']=json.loads(pending.read_text('utf-8'))['target'];result['restart_required']=True
    except (OSError,ValueError,KeyError):result['restart_required']=False
    return result

@router.get('/setup')
def setup():return read_setup()

class SetupBody(BaseModel):
    completed:bool|None=None
    source_dirs:list[str]|None=Field(default=None,max_length=20)
    data_dir:str|None=None
    export_dir:str|None=None
    language:str|None=None
    citation_style:str|None=None
    reduce_motion:bool|None=None

def directory(value,exist=False):
    p=Path(value).expanduser()
    if not p.is_absolute():raise HTTPException(422,'目录需要绝对路径')
    p=p.resolve()
    if p==Path(p.anchor):raise HTTPException(422,'请选择磁盘下的具体文件夹')
    if exist and not p.is_dir():raise HTTPException(422,f'资料目录不存在：{p}')
    if p.exists() and not p.is_dir():raise HTTPException(422,'路径指向文件，请选择目录')
    return p

@router.put('/setup')
def save_setup(body:SetupBody):
    current=read_setup();changes=body.model_dump(exclude_none=True)
    if 'source_dirs' in changes:changes['source_dirs']=list(dict.fromkeys(str(directory(v,True)) for v in changes['source_dirs']))
    if 'language' in changes and changes['language'] not in ('zh','en','both'):raise HTTPException(422,'请选择中文、英文或双语')
    if changes.get('citation_style')=='gbt7714':changes['citation_style']='gbt'
    if 'citation_style' in changes and changes['citation_style'] not in ('apa','ieee','gbt'):raise HTTPException(422,'不支持的引用样式')
    if 'export_dir' in changes:changes['export_dir']=str(directory(changes['export_dir']))
    target=directory(changes.pop('data_dir')) if 'data_dir' in changes else None
    if target is not None and target!=core.DATA_DIR:
        if target.is_relative_to(core.DATA_DIR) or core.DATA_DIR.is_relative_to(target):raise HTTPException(422,'新旧数据目录不能相互包含')
        if target.exists() and any(target.iterdir()):raise HTTPException(409,'新数据目录需要为空，以免覆盖其他资料')
        pending=core.ROOT/'.state'/'storage-request.json';pending.parent.mkdir(parents=True,exist_ok=True)
        pending.write_text(json.dumps({'source':str(core.DATA_DIR),'target':str(target),'requested_at':now()},ensure_ascii=False),'utf-8')
    elif target is not None:
        (core.ROOT/'.state'/'storage-request.json').unlink(missing_ok=True)
    saved={k:current[k] for k in ('completed','source_dirs','language','citation_style','reduce_motion') if k in current};saved.update(changes)
    with conn() as db:
        db.execute("INSERT INTO settings VALUES ('workspace_setup',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(json.dumps(saved,ensure_ascii=False),))
        if 'export_dir' in changes:db.execute("INSERT INTO settings VALUES ('export_dir',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(json.dumps(changes['export_dir']),))
    return read_setup()

@router.get('/setup/folders')
def browse_folders(path:str=''):
    if not path:
        roots=[f'{c}:\\' for c in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ' if Path(f'{c}:\\').exists()] if os.name=='nt' else ['/']
        return {'path':'','parent':None,'items':[{'name':p,'path':p} for p in roots]}
    p=Path(path).expanduser()
    if not p.is_absolute() or not p.is_dir():raise HTTPException(422,'目录不存在')
    try:
        items=[{'name':c.name,'path':str(c)} for c in p.iterdir() if c.is_dir() and not c.is_symlink() and not c.name.startswith('.')]
        return {'path':str(p.resolve()),'parent':str(p.parent) if p.parent!=p else '', 'items':sorted(items,key=lambda i:i['name'].lower())[:1000]}
    except OSError:raise HTTPException(403,'无法读取该目录')

@router.post('/setup/folder-picker')
def choose_folder():
    if os.name!='nt':raise HTTPException(501,'当前系统请使用目录浏览器或输入路径')
    script="Add-Type -AssemblyName System.Windows.Forms; $d=New-Object System.Windows.Forms.FolderBrowserDialog; $d.Description='选择工作台目录'; if($d.ShowDialog() -eq 'OK'){ [Console]::OutputEncoding=[Text.Encoding]::UTF8; [Console]::Write($d.SelectedPath) }"
    encoded=base64.b64encode(script.encode('utf-16-le')).decode()
    try:
        result=subprocess.run(['powershell.exe','-NoProfile','-STA','-EncodedCommand',encoded],capture_output=True,timeout=180,creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode:raise HTTPException(503,'系统目录选择器不可用，请在表单输入路径')
        return {'path':result.stdout.decode('utf-8-sig').strip(),'cancelled':not bool(result.stdout.strip())}
    except subprocess.TimeoutExpired:raise HTTPException(408,'目录选择超时，请重试')
