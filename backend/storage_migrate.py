"""Run while the server is stopped, before starting the new process."""
from __future__ import annotations
import json,os,shutil,sqlite3
from pathlib import Path
from . import core

def apply_pending():
    request=core.ROOT/'.state'/'storage-request.json'
    if not request.exists():return False
    data=json.loads(request.read_text('utf-8'));source=Path(data['source']).resolve();target=Path(data['target']).resolve()
    if source!=core.DATA_DIR:raise RuntimeError('数据路径已改变；请在设置中重新选择迁移目录')
    if target==Path(target.anchor) or target.is_relative_to(source) or source.is_relative_to(target):raise RuntimeError('不安全的数据迁移目标')
    if target.exists() and any(target.iterdir()):raise RuntimeError('目标目录非空；没有覆盖文件')
    target.mkdir(parents=True,exist_ok=True)
    try:
        for item in source.iterdir():
            if item.name.startswith('workbench.sqlite3'):continue
            if item.is_symlink():continue
            if item.is_dir():shutil.copytree(item,target/item.name)
            else:shutil.copy2(item,target/item.name)
        dbfile=source/'workbench.sqlite3'
        if dbfile.exists():
            with sqlite3.connect(dbfile) as src,sqlite3.connect(target/'workbench.sqlite3') as dst:
                src.backup(dst)
                if dst.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise RuntimeError('迁移后的数据库校验失败')
        config=core.ROOT/'.state'/'storage.json';temp=config.with_suffix('.tmp')
        temp.write_text(json.dumps({'data_dir':str(target)},ensure_ascii=False),'utf-8');os.replace(temp,config)
        request.unlink();return True
    except Exception:
        # Original is retained; partial target remains for manual inspection.
        raise

if __name__=='__main__':
    if apply_pending():print('数据已迁移并校验，原目录保留。')
