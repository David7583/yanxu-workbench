import hashlib,json
import pytest
from fastapi import HTTPException
from backend import core,core_routes,archive_routes as archive,importer

@pytest.fixture
def workspace(tmp_path,monkeypatch):
    monkeypatch.setattr(core,'DATA_DIR',tmp_path/'data');core.init_db();archive.init_schema()
    monkeypatch.setattr(importer.POOL,'submit',lambda *a:None)
    yield tmp_path
    importer.CANCELLED.clear();importer.WORKERS.clear()

def make_job(identifier,legacy=False):
    directory=core.DATA_DIR/'imports';directory.mkdir(parents=True,exist_ok=True)
    path=directory/(identifier+'.json')
    raw=[{'id':str(i),'provider':'Test','messages':[{'id':'m','role':'user','content':str(i)}]} for i in range(3)]
    data=json.dumps(raw).encode();path.write_bytes(data)
    with core.conn() as db:
        db.execute('INSERT INTO imports(id,filename,status,created_at,updated_at,file_hash,bytes_total,source_path) VALUES (?,?,?,?,?,?,?,?)',(identifier,'archive.json','cancelled' if legacy else 'queued',core.now(),core.now(),hashlib.sha256(data).hexdigest(),len(data),None if legacy else str(path)))
    return path,raw

def test_legacy_upload_resume_is_idempotent(workspace):
    path,raw=make_job('old',legacy=True)
    importer.store_conversation(importer.normalize_conversation(raw[0],'archive.json'),str(path),'archive.json')
    with core.conn() as db:db.execute("UPDATE imports SET total=1,imported=1 WHERE id='old'")
    assert core_routes.import_status('old')['can_resume']
    assert archive.resume_archive('old')['checkpoint']==1
    with pytest.raises(HTTPException) as error:archive.resume_archive('old')
    assert error.value.status_code==409
    archive.process_archive('old')
    job=core_routes.import_status('old');assert job['status']=='done';assert job['total']==3;assert job['imported']==3
    with core.conn() as db:assert db.execute('SELECT COUNT(*) FROM source_messages').fetchone()[0]==3

def test_single_upload_pause_resume_keeps_checkpoint(workspace,monkeypatch):
    path,_=make_job('new');original=importer.store_conversation
    def store_and_pause(*args):
        result=original(*args);core_routes.cancel_import('new');return result
    monkeypatch.setattr(importer,'store_conversation',store_and_pause)
    importer.process_import('new',path,'archive.json')
    job=core_routes.import_status('new');assert job['status']=='cancelled';assert job['checkpoint']==1;assert job['can_resume']
    monkeypatch.setattr(importer,'store_conversation',original)
    archive.resume_archive('new');archive.process_archive('new')
    job=core_routes.import_status('new');assert job['status']=='done';assert job['imported']==3

def test_soft_delete_restore_and_private_dashboard(workspace):
    project=core_routes.projects()['items'][0];identifier=project['id']
    core.update_project(identifier,{'expected_revision':0,'notes':'PRIVATE_CONTENT','draft_zh':'PRIVATE_DRAFT'})
    with core.conn() as db:db.execute('INSERT INTO chats(id,project_id,role,content,created_at) VALUES (?,?,?,?,?)',('chat',identifier,'user','PRIVATE_CHAT',core.now()))
    dashboard=core_routes.dashboard();assert len(dashboard['activity'])==7;assert dashboard['projects'][0]['progress']==75
    assert 'PRIVATE_' not in json.dumps(dashboard)
    core_routes.delete_project(identifier);assert core_routes.projects()['items']==[]
    assert len(core_routes.projects(deleted=True)['items'])==1
    with pytest.raises(HTTPException):core.update_project(identifier,{'notes':'bad'})
    assert core_routes.dashboard()['counts']['deleted_projects']==1
    restored=core_routes.restore_deleted_project(identifier);assert restored['notes']=='PRIVATE_CONTENT'
