import asyncio
import json
import time
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from backend import core, providers, provider_routes as pr, group_routes as gr, research_routes as rr

@pytest.fixture
def env(tmp_path,monkeypatch):
    monkeypatch.setattr(core,'DATA_DIR',tmp_path/'data');core.init_db()
    vault={}
    monkeypatch.setattr(providers.keyring,'get_password',lambda svc,k:vault.get((svc,k)))
    monkeypatch.setattr(providers.keyring,'set_password',lambda svc,k,v:vault.__setitem__((svc,k),v))
    monkeypatch.setattr(providers.keyring,'delete_password',lambda svc,k:vault.pop((svc,k),None))
    providers.init_schema();gr.init_schema()
    app=FastAPI();app.include_router(pr.router);app.include_router(gr.router);app.include_router(rr.router)
    with core.conn() as db: pid=db.execute('SELECT id FROM projects').fetchone()[0]
    with TestClient(app) as client:yield client,pid,vault


def profile(client,name='One',**extra):
    response=client.post('/api/providers',json={'catalog_id':'custom','name':name,'base_url':'https://example.org/v1','model':'model-A','api_key':'secret-'+name,**extra})
    assert response.status_code==200,response.text
    return response.json()


def discussion(client,pid,profiles,kind='group'):
    response=client.post(f'/api/projects/{pid}/sessions',json={'title':'证据讨论','kind':kind,'members':[{'name':p['name'],'provider_id':p['id'],'model':p['model']} for p in profiles]})
    assert response.status_code==200,response.text
    return response.json()['id']


def wait_done(client,sid):
    for _ in range(100):
        detail=client.get('/api/sessions/'+sid).json()
        if detail['runs'] and detail['runs'][0]['status'] not in {'queued','running'}:return detail
        time.sleep(.02)
    pytest.fail('run did not finish')


def test_profiles_keys_are_independent_even_same_endpoint(env):
    c,pid,vault=env;a=profile(c,'A');b=profile(c,'B')
    assert len(vault)==2
    assert providers.api_key(providers.get_profile(a['id']))=='secret-A'
    assert providers.api_key(providers.get_profile(b['id']))=='secret-B'
    assert 'secret-' not in c.get('/api/providers').text
    with core.conn() as db:assert 'secret-' not in str([tuple(x) for x in db.execute('SELECT * FROM provider_profiles')])
    c.put('/api/providers/'+a['id'],json={'base_url':'https://different.org/v1'})
    assert c.get('/api/providers').json()['items'][0]['has_key'] is False
    assert providers.api_key(providers.get_profile(b['id']))=='secret-B'


def test_catalog_hidden_and_custom_profile_lifecycle(env):
    c,pid,vault=env
    assert len(c.get('/api/provider-catalog').json()['items'])>=25
    assert c.delete('/api/provider-catalog/openai').status_code==200
    assert 'openai' not in [i['id'] for i in c.get('/api/provider-catalog').json()['items']]
    c.post('/api/provider-catalog/openai/restore')
    a=profile(c)
    c.put('/api/providers/'+a['id'],json={'enabled':False})
    assert c.post('/api/providers/'+a['id']+'/default').status_code==409
    c.delete('/api/providers/'+a['id'])
    assert not c.get('/api/providers').json()['items']
    assert not vault
    providers.init_schema()
    assert not c.get('/api/providers').json()['items']


def test_legacy_migration_preserves_key_and_settings(env):
    c,pid,vault=env
    with core.conn() as db:
        db.execute("DELETE FROM settings WHERE key='providers_migrated'")
        db.execute("INSERT OR REPLACE INTO settings VALUES('model','\"legacy-model\"')")
    old=providers.settings();providers.keyring.set_password(providers.SERVICE,providers.credential_name(old),'legacy-key')
    providers.init_schema()
    cfg=providers.settings()
    assert cfg['model']=='legacy-model' and cfg['provider_id']
    assert providers.api_key(cfg)=='legacy-key'
    providers.init_schema()
    assert len(c.get('/api/providers').json()['items'])==1


@pytest.mark.parametrize('protocol,endpoint,reply,header',[
 ('openai-compatible','/chat/completions',{'choices':[{'message':{'content':'OK'},'finish_reason':'stop'}]},'authorization'),
 ('openai-responses','/responses',{'status':'completed','output':[{'type':'message','content':[{'type':'output_text','text':'OK'}]}]},'authorization'),
 ('anthropic','/messages',{'content':[{'type':'text','text':'OK'}],'stop_reason':'end_turn'},'x-api-key'),
 ('gemini','/models/model-A:generateContent',{'candidates':[{'content':{'parts':[{'text':'hidden','thought':True},{'text':'OK'}]},'finishReason':'STOP'}]},'x-goog-api-key'),
])
def test_native_protocols(env,monkeypatch,protocol,endpoint,reply,header):
    c,pid,vault=env;p=profile(c,protocol=protocol)
    original=providers.httpx.AsyncClient
    def factory(**kwargs):
        async def handler(req):
            assert req.url.path=='/v1'+endpoint
            assert 'secret-One' in req.headers[header]
            payload=json.loads(req.content)
            if protocol=='openai-responses':assert payload['store'] is False
            return providers.httpx.Response(200,json=reply)
        return original(transport=providers.httpx.MockTransport(handler),**kwargs)
    monkeypatch.setattr(providers.httpx,'AsyncClient',factory)
    result=c.post('/api/providers/'+p['id']+'/test')
    assert result.status_code==200,result.text
    assert result.json()['response']=='OK'


def test_model_discovery_without_selected_model(env,monkeypatch):
    c,pid,vault=env;p=profile(c,protocol='gemini',model='')
    calls=[]
    async def fake(config,method,endpoint,payload=None):
        calls.append(endpoint)
        if len(calls)==1:return {'models':[{'name':'models/model-a','supportedGenerationMethods':['generateContent']}],'nextPageToken':'next'}
        return {'models':[{'name':'models/embed','supportedGenerationMethods':['embedContent']}]}
    monkeypatch.setattr(providers,'request_provider',fake)
    result=c.get('/api/providers/'+p['id']+'/models')
    assert result.status_code==200,result.text
    assert len(calls)==2 and 'pageToken=next' in calls[1]
    assert result.json()['items'][0]['chat_supported'] is False


def test_atomic_creation_and_direct_limits(env):
    c,pid,vault=env;p=profile(c)
    result=c.post(f'/api/projects/{pid}/sessions',json={'title':'Invalid','kind':'direct','members':[{'name':'X','provider_id':p['id'],'model':'x'},{'name':'Y','provider_id':p['id'],'model':'y'}]})
    assert result.status_code==422
    assert c.get(f'/api/projects/{pid}/sessions').json()['items']==[]


def test_group_rounds_see_prior_round_and_preserve_authorship(env,monkeypatch):
    c,pid,vault=env;a=profile(c,'A');b=profile(c,'B');sid=discussion(c,pid,[a,b]);captured=[]
    async def answer(messages,**kwargs):
        captured.append((kwargs['config']['name'],messages))
        await asyncio.sleep(.01)
        return kwargs['config']['name']+' evidence [H999]'
    monkeypatch.setattr(providers,'complete',answer)
    sent=c.post('/api/sessions/'+sid+'/messages',json={'message':'讨论研究方法','mode':'moderated','rounds':2,'use_history':False})
    assert sent.status_code==200,sent.text
    detail=wait_done(c,sid)
    assert detail['runs'][0]['completed']==4
    answers=[m for m in detail['messages'] if m['role']=='assistant']
    assert {m['name'] for m in answers}=={'A','B'} and all(m['model']=='model-A' for m in answers)
    assert all('[来源未匹配]' in m['content'] for m in answers)
    assert 'A evidence' in str(captured[2:]) and 'B evidence' in str(captured[2:])
    c.delete('/api/providers/'+a['id'])
    assert c.get('/api/sessions/'+sid).json()['messages'][1]['provider_name'] in {'A','B'}
    c.post('/api/sessions/'+sid+'/dissolve')
    assert c.post('/api/sessions/'+sid+'/messages',json={'message':'again','mode':'all'}).status_code==409
    assert len(c.get('/api/sessions/'+sid).json()['messages'])==5


def test_stop_cancels_pending_reply_and_prevents_overlap(env,monkeypatch):
    c,pid,vault=env;p=profile(c);sid=discussion(c,pid,[p],'direct')
    async def answer(*args,**kwargs):await asyncio.sleep(10);return 'late reply'
    monkeypatch.setattr(providers,'complete',answer)
    c.post('/api/sessions/'+sid+'/messages',json={'message':'start','mode':'all','use_history':False})
    assert c.post('/api/sessions/'+sid+'/messages',json={'message':'overlap','mode':'all'}).status_code==409
    time.sleep(.05)
    assert c.post('/api/sessions/'+sid+'/stop').status_code==200
    detail=wait_done(c,sid)
    assert detail['runs'][0]['status']=='cancelled'
    assert not any(m['content']=='late reply' or m['status']=='pending' for m in detail['messages'])


def test_legacy_chat_migrates_once_without_inventing_model(env):
    c,pid,vault=env
    with core.conn() as db:db.execute("INSERT INTO chats VALUES(?,?,?,?,?,?)",('old',pid,'assistant','原有研究内容',core.now(),'[]'))
    first=c.get(f'/api/projects/{pid}/sessions').json()['items']
    second=c.get(f'/api/projects/{pid}/sessions').json()['items']
    assert len(first)==len(second)==1
    msg=c.get('/api/sessions/'+first[0]['id']).json()['messages'][0]
    assert msg['content']=='原有研究内容' and not msg['model'] and msg['metadata']['legacy']


def test_generation_uses_selected_group_not_another_project(env,monkeypatch):
    c,pid,vault=env;p=profile(c);sid=discussion(c,pid,[p])
    with core.conn() as db:db.execute("INSERT INTO session_messages(id,session_id,role,name,content,created_at) VALUES(?,?,?,?,?,?)",('turn',sid,'assistant','Reviewer','Unique group insight',core.now()))
    async def answer(messages,**kwargs):
        assert 'Unique group insight' in str(messages)
        return '{"notes":"整理建议"}'
    monkeypatch.setattr(providers,'complete',answer)
    r=c.post(f'/api/projects/{pid}/generate',json={'session_id':sid,'target':'notes','expected_revision':0})
    assert r.status_code==200,r.text
    assert r.json()['generation_metadata']['session_id']==sid
    assert core.get_project(pid)['notes']==''


def test_profile_delete_cleans_previous_endpoint_bindings(env):
    c,pid,vault=env;p=profile(c)
    c.put('/api/providers/'+p['id'],json={'base_url':'https://another.org/v1','api_key':'other-secret'})
    assert len(vault)==2
    c.delete('/api/providers/'+p['id'])
    assert not vault


def test_partial_failure_retry_only_failed_member_and_host_summary(env,monkeypatch):
    c,pid,vault=env;a=profile(c,'A');b=profile(c,'B');sid=discussion(c,pid,[a,b]);calls=[];fail=True
    targets=c.get('/api/sessions/'+sid).json()['members']
    async def answer(messages,**kwargs):
        name=kwargs['config']['name'];calls.append((name,str(messages)))
        if name=='B' and fail:raise providers.HTTPException(502,'暂时不可用')
        return name+' answer'
    monkeypatch.setattr(providers,'complete',answer)
    c.post('/api/sessions/'+sid+'/messages',json={'message':'original question','mode':'moderated','rounds':1,'moderator_id':targets[0]['id'],'use_history':False})
    detail=wait_done(c,sid)
    assert detail['runs'][0]['status']=='partial' and detail['runs'][0]['total']==3
    assert '主持人' in calls[-1][1] and 'A answer' in calls[-1][1]
    failed=next(m for m in detail['messages'] if m['status']=='failed');fail=False;calls.clear()
    result=c.post('/api/sessions/'+sid+'/retry',json={'message_id':failed['id']})
    assert result.status_code==200,result.text
    detail=wait_done(c,sid)
    assert len(calls)==1 and calls[0][0]=='B' and 'original question' in calls[0][1]
    assert detail['runs'][0]['status']=='completed'
    assert next(m for m in detail['messages'] if m['id']==failed['id'])['status']=='failed'


def test_http_agent_is_real_member_without_model_profile(env,monkeypatch):
    from backend import skills_routes as skills
    c,pid,vault=env
    agent=skills.create_agent(skills.AgentBody(name='Remote helper',kind='http',endpoint='https://agent.example/api',api_key='agent-key',instructions='Research responsibly'))
    result=c.post(f'/api/projects/{pid}/sessions',json={'title':'Agent discussion','kind':'direct','members':[{'name':'Remote helper','agent_id':agent['id']}]})
    assert result.status_code==200,result.text
    sid=result.json()['id'];calls=[]
    async def invoke(aid,messages,project_id=None,context=None,**kwargs):
        calls.append((aid,project_id,messages));return 'Remote actual answer'
    async def no_provider(*args,**kwargs):pytest.fail('HTTP agent should not use provider completion')
    monkeypatch.setattr(skills,'invoke_agent',invoke);monkeypatch.setattr(providers,'complete',no_provider)
    result=c.post('/api/sessions/'+sid+'/messages',json={'message':'help','mode':'all','use_history':False})
    assert result.status_code==200,result.text
    detail=wait_done(c,sid)
    assert detail['runs'][0]['status']=='completed'
    msg=next(m for m in detail['messages'] if m['role']=='assistant')
    assert msg['content']=='Remote actual answer' and msg['metadata']['agent_id']==agent['id']
    assert calls[0][1]==pid


def test_local_agent_uses_enabled_skill_and_records_version(env,monkeypatch):
    from backend import skills_routes as skills
    c,pid,vault=env;p=profile(c)
    skill=skills.create_skill(skills.SkillBody(name='evidence-check',description='check evidence',content='Unique evidence workflow',enabled=True))
    agent=skills.create_agent(skills.AgentBody(name='Critic',provider_id=p['id'],model=p['model'],skill_ids=[skill['id']]))
    result=c.post(f'/api/projects/{pid}/sessions',json={'title':'Skill discussion','kind':'direct','members':[{'name':'Critic','agent_id':agent['id']}]})
    sid=result.json()['id']
    async def answer(messages,**kwargs):
        assert 'Unique evidence workflow' in str(messages)
        return 'Checked'
    monkeypatch.setattr(providers,'complete',answer)
    c.post('/api/sessions/'+sid+'/messages',json={'message':'evaluate','mode':'all','use_history':False})
    detail=wait_done(c,sid)
    msg=next(m for m in detail['messages'] if m['role']=='assistant')
    assert msg['metadata']['skills'][0]['id']==skill['id']
    assert msg['metadata']['skills'][0]['version']==1


def test_transcript_does_not_reassign_old_source_numbers(env):
    c,pid,vault=env;p=profile(c);sid=discussion(c,pid,[p]);member=c.get('/api/sessions/'+sid).json()['members'][0]
    with core.conn() as db:db.execute("INSERT INTO session_messages(id,session_id,role,name,content,created_at,sources) VALUES(?,?,?,?,?,?,?)",('old-source',sid,'assistant','Old speaker','Original claim [H1]',core.now(),json.dumps([{'label':'H1','title':'Original evidence','id':'old-archive'}])))
    context=gr.transcript(sid,member)
    assert '[H1]' not in context[-1]['content']
    assert 'Original evidence' in context[-1]['content'] and 'old-archive' in context[-1]['content']
