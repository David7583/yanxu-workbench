import json,threading,zipfile
from pathlib import Path
import pytest
from fastapi import HTTPException
from backend import core,importer,archive_routes as archives,skills_routes as skills,setup_routes as setup

@pytest.fixture
def db(tmp_path,monkeypatch):
    monkeypatch.setattr(core,'DATA_DIR',tmp_path/'data');monkeypatch.setattr(core,'ROOT',tmp_path)
    core.init_db();archives.init_schema();skills.init_schema()
    secrets={}
    monkeypatch.setattr(skills.keyring,'get_password',lambda s,k:secrets.get((s,k)))
    monkeypatch.setattr(skills.keyring,'set_password',lambda s,k,v:secrets.__setitem__((s,k),v))
    return tmp_path

def test_message_model_time_and_old_export_preserved(db):
    def record(text,time):return importer.normalize_conversation({'id':'x','provider':'Test','updated_at':time,'messages':[{'id':'m','role':'assistant','model':'model-A','created_at':'2025-01-01T08:00:00+08:00','updated_at':time,'content':text}]},'sample.json')
    newer=record('new finding','2026-02-01T00:00:00Z');older=record('old finding','2025-02-01T00:00:00Z')
    assert importer.store_conversation(newer,'new.json','new.json')
    assert not importer.store_conversation(older,'old.json','old.json')
    with core.conn() as c:
        row=c.execute('SELECT * FROM source_messages').fetchone()
        assert row['content']=='new finding';assert row['model']=='model-A';assert row['original_time'].endswith('+08:00')
        assert c.execute('SELECT COUNT(*) FROM source_message_versions').fetchone()[0]==2

def test_platforms_not_conflated_by_same_text(db):
    for provider in ['Claude','ChatGPT']:
        importer.store_conversation(importer.normalize_conversation({'provider':provider,'id':'id1','messages':[{'role':'user','content':'same text'}]},'a.json'),'a.json','a.json')
    with core.conn() as c:assert c.execute('SELECT COUNT(*) FROM sources').fetchone()[0]==2

def test_deepseek_fragments_and_grok_structure():
    data={'id':'d','mapping':{'1':{'parent':None,'message':{'model':'deepseek','fragments':[{'type':'REQUEST','content':'q'}]}},'2':{'parent':'1','message':{'model':'deepseek','fragments':[{'type':'THINK','content':'reason'},{'type':'RESPONSE','content':'answer'}]}}}}
    value=importer.normalize_conversation(data,'deepseek.json')
    assert value['provider']=='DeepSeek';assert [m['role'] for m in value['messages']]==['user','assistant','assistant']
    assert value['messages'][2]['parent_id']=='2:0';assert value['messages'][1]['metadata']['channel']=='THINK'
    grok=importer.normalize_conversation({'conversation':{'id':'g','title':'title'},'responses':[{'response':{'_id':'r','sender':'human','message':'hello','model':'grok','create_time':'2026-01-01'}}]},'prod-grok-backend.json')
    assert grok['provider']=='Grok';assert grok['messages'][0]['role']=='user';assert grok['messages'][0]['model']=='grok'

def test_qwen_sequence_is_one_conversation(db):
    p=db/'qwen.json';p.write_text(json.dumps([{'user':'q'},{'assistant':'a'}]),'utf-8')
    results=list(importer.iter_json(p,p.name));assert len(results)==1
    assert results[0][0]['provider']=='千问';assert results[0][0]['messages'][0]['created_at'] is None

def test_zip_scan_filters_account_and_stream_import(db):
    root=db/'exports';root.mkdir();p=root/'backup.zip'
    with zipfile.ZipFile(p,'w') as z:
        z.writestr('conversations-000.json',json.dumps([{'id':'x','messages':[{'role':'user','content':'research'}]}]))
        z.writestr('user.json','{"email":"not imported"}');z.writestr('../conversations.json','[]');z.writestr('run.py','raise Exception()')
    preview=archives.scan_archives(archives.ScanBody(paths=[str(root)]))
    assert preview['items'][0]['members']==1
    assert len(list(archives.archive_records(p,threading.Event())))==1
    assert not (db/'run.py').exists()

def test_resume_replays_without_duplication(db,monkeypatch):
    root=db/'exports';root.mkdir();p=root/'conversations.json';p.write_text(json.dumps([{'id':str(n),'messages':[{'role':'user','content':str(n)}]} for n in range(3)]),'utf-8')
    preview=archives.scan_archives(archives.ScanBody(paths=[str(root)]))
    monkeypatch.setattr(importer.POOL,'submit',lambda *a:None)
    job=archives.import_archives(archives.ImportBody(scan_id=preview['scan_id'],item_ids=[preview['items'][0]['id']]))['jobs'][0]['job_id']
    archives.process_archive(job)
    with core.conn() as c:
        assert c.execute('SELECT COUNT(*) FROM sources').fetchone()[0]==3
        c.execute("UPDATE imports SET status='interrupted',checkpoint=1 WHERE id=?",(job,))
    archives.resume_archive(job);archives.process_archive(job)
    with core.conn() as c:assert c.execute('SELECT COUNT(*) FROM sources').fetchone()[0]==3

def test_skills_versions_and_agent_context(db):
    item=skills.create_skill(skills.SkillBody(name='review',content='Check evidence',enabled=True))
    updated=skills.edit_skill(item['id'],skills.SkillBody(name='review',content='Check evidence and counterexamples',enabled=True,expected_version=1))
    assert updated['version']==2
    with pytest.raises(HTTPException):skills.edit_skill(item['id'],skills.SkillBody(name='review',content='stale',expected_version=1))
    agent=skills.create_agent(skills.AgentBody(name='Reviewer',instructions='Review critically',skill_ids=[item['id']]))
    context=skills.get_agent_context(agent['id']);assert 'counterexamples' in context['instructions'];assert context['skills'][0]['version']==2
    restored=skills.restore(item['id'],1);assert restored['version']==3;assert restored['content']=='Check evidence'
    skills.delete_skill(item['id']);assert skills.get_agent_context(agent['id'])['skills']==[]

def test_skill_scan_never_executes_scripts(db):
    folder=db/'skill';folder.mkdir();(folder/'scripts').mkdir()
    (folder/'SKILL.md').write_text('---\nname: review\ndescription: Review manuscript\n---\nReview sources','utf-8')
    (folder/'scripts'/'bad.py').write_text('raise Exception("never run")','utf-8')
    result=skills.scan_skills(skills.ScanBody(paths=[str(folder)]));item=result['items'][0]
    assert not item['enabled'];assert item['metadata']['has_scripts']
    assert skills.scan_skills(skills.ScanBody(paths=[str(folder)]))['items'][0]['id']==item['id']

def test_http_credentials_bound_to_endpoint(db):
    agent=skills.create_agent(skills.AgentBody(name='Remote',kind='http',endpoint='https://example.org/agent',api_key='secret'))
    assert agent['has_key'];assert 'secret' not in json.dumps(agent)
    changed=skills.edit_agent(agent['id'],skills.AgentBody(name='Remote',kind='http',endpoint='https://other.example/agent'))
    assert not changed['has_key'];assert skills.get_agent_context(agent['id'])['kind']=='http'
    with pytest.raises(HTTPException):skills.create_agent(skills.AgentBody(name='bad',kind='http',endpoint='http://remote.example/agent'))

def test_setup_plan_does_not_move_live_data(db):
    source=db/'raw';source.mkdir();target=db/'moved'
    result=setup.save_setup(setup.SetupBody(completed=True,source_dirs=[str(source)],data_dir=str(target),citation_style='gbt7714'))
    assert result['restart_required'];assert result['citation_style']=='gbt';assert not target.exists()
    assert core.DATA_DIR.joinpath('workbench.sqlite3').exists()
    setup.save_setup(setup.SetupBody(completed=True))
    assert setup.read_setup()['restart_required']
