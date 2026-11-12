"""Provider registry, native protocol presets and independent credential profiles."""
from __future__ import annotations
import json
from typing import Literal
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, ConfigDict
from .core import conn, now, uid
from . import providers

router = APIRouter(prefix="/api")
init_schema = providers.init_schema

# Editable connection templates, not a claim of tested account/model availability.
_PRESETS = [
 ("openai", "OpenAI", "openai-responses", "https://api.openai.com/v1", "https://developers.openai.com/api/reference/resources/models/methods/list", "全球"),
 ("anthropic", "Anthropic · Claude", "anthropic", "https://api.anthropic.com/v1", "https://platform.claude.com/docs/en/api/models/list", "全球"),
 ("google", "Google · Gemini", "gemini", "https://generativelanguage.googleapis.com/v1beta", "https://ai.google.dev/api/models", "全球"),
 ("deepseek", "DeepSeek", "openai-compatible", "https://api.deepseek.com", "https://api-docs.deepseek.com/", "中国"),
 ("qwen", "阿里云百炼 · 通义千问", "openai-compatible", "https://dashscope.aliyuncs.com/compatible-mode/v1", "https://help.aliyun.com/zh/model-studio/compatibility-of-openai-with-dashscope", "中国 · 可改为控制台区域地址"),
 ("qwen-intl", "Alibaba Cloud Model Studio", "openai-compatible", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1", "https://www.alibabacloud.com/help/en/model-studio/compatibility-of-openai-with-dashscope", "国际 · 可改为控制台工作空间地址"),
 ("xai", "xAI · Grok", "openai-compatible", "https://api.x.ai/v1", "https://docs.x.ai/docs/api-reference", "全球"),
 ("moonshot", "月之暗面 · Kimi", "openai-compatible", "https://api.moonshot.cn/v1", "https://platform.moonshot.cn/docs/api/chat", "中国"),
 ("zhipu", "智谱 · GLM", "openai-compatible", "https://open.bigmodel.cn/api/paas/v4", "https://docs.bigmodel.cn/cn/api/introduction", "中国"),
 ("zai", "Z.ai · GLM", "openai-compatible", "https://api.z.ai/api/paas/v4", "https://docs.z.ai/guides/overview/quick-start", "国际"),
 ("ark", "火山方舟 · 豆包", "openai-compatible", "https://ark.cn-beijing.volces.com/api/v3", "https://www.volcengine.com/docs/82379/1330626", "中国 · 部分账户需填写推理接入点 ID"),
 ("hunyuan", "腾讯混元", "openai-compatible", "https://api.hunyuan.cloud.tencent.com/v1", "https://cloud.tencent.com/document/product/1729/111007", "中国"),
 ("qianfan", "百度千帆", "openai-compatible", "https://qianfan.baidubce.com/v2", "https://cloud.baidu.com/doc/qianfan-api/s/3m7of64lb", "中国"),
 ("minimax", "MiniMax", "openai-compatible", "https://api.minimax.io/v1", "https://platform.minimax.io/docs/api-reference/text-openai-api", "国际 · 中国账户请修改区域地址"),
 ("stepfun", "阶跃星辰 · StepFun", "openai-compatible", "https://api.stepfun.com/v1", "https://platform.stepfun.com/docs/overview/concept", "中国"),
 ("siliconflow", "硅基流动 · SiliconFlow", "openai-compatible", "https://api.siliconflow.cn/v1", "https://docs.siliconflow.cn/en/api-reference/chat-completions/chat-completions", "中国"),
 ("mistral", "Mistral AI", "openai-compatible", "https://api.mistral.ai/v1", "https://docs.mistral.ai/api/endpoint/models", "全球"),
 ("groq", "Groq", "openai-compatible", "https://api.groq.com/openai/v1", "https://console.groq.com/docs/openai", "全球"),
 ("openrouter", "OpenRouter", "openai-compatible", "https://openrouter.ai/api/v1", "https://openrouter.ai/docs/api-reference/overview", "聚合服务"),
 ("together", "Together AI", "openai-compatible", "https://api.together.xyz/v1", "https://docs.together.ai/docs/openai-api-compatibility", "全球"),
 ("fireworks", "Fireworks AI", "openai-compatible", "https://api.fireworks.ai/inference/v1", "https://docs.fireworks.ai/tools-sdks/openai-compatibility", "全球"),
 ("nvidia", "NVIDIA NIM", "openai-compatible", "https://integrate.api.nvidia.com/v1", "https://docs.api.nvidia.com/nim/reference/llm-apis", "全球"),
 ("perplexity", "Perplexity", "openai-compatible", "https://api.perplexity.ai", "https://docs.perplexity.ai/guides/chat-completions-guide", "全球 · 列表接口可能需手动模型 ID"),
 ("ollama", "Ollama", "openai-compatible", "http://localhost:11434/v1", "https://docs.ollama.com/api/openai-compatibility", "本机"),
 ("lmstudio", "LM Studio", "openai-compatible", "http://localhost:1234/v1", "https://lmstudio.ai/docs/developer/openai-compat", "本机"),
 ("custom", "自定义服务 / 企业网关", "openai-compatible", "https://api.example.com/v1", "", "自定义"),
]
CATALOG = [{"id": i,"name": n,"protocol": p,"base_url": b,"docs_url": d,"region": r,"requires_key": i not in {"ollama","lmstudio"},"notice": "接口地址可编辑；具体区域、模型与账户权限以服务商控制台为准。"} for i,n,p,b,d,r in _PRESETS]

@router.get("/provider-catalog")
def catalog(include_hidden: bool = False):
    with conn() as db: hidden={r[0] for r in db.execute("SELECT id FROM provider_catalog_hidden")}
    return {"items": [p | {"hidden": p["id"] in hidden} for p in CATALOG if include_hidden or p["id"] not in hidden]}

@router.delete("/provider-catalog/{identifier}")
def hide_catalog(identifier: str):
    if identifier not in {p["id"] for p in CATALOG}: raise HTTPException(404,"厂商模板不存在。")
    with conn() as db: db.execute("INSERT OR IGNORE INTO provider_catalog_hidden VALUES(?)",(identifier,))
    return {"ok":True}

@router.post("/provider-catalog/{identifier}/restore")
def restore_catalog(identifier: str):
    with conn() as db: db.execute("DELETE FROM provider_catalog_hidden WHERE id=?",(identifier,))
    return {"ok":True}

class ProfileInput(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str | None = Field(default=None,min_length=1,max_length=100)
    catalog_id: str | None = Field(default=None,max_length=100)
    protocol: Literal["openai-compatible","openai-responses","anthropic","gemini"] | None = None
    base_url: str | None = Field(default=None,max_length=2000)
    model: str | None = Field(default=None,max_length=300)
    models: list[str] | None = Field(default=None,max_length=300)
    api_key: str | None = Field(default=None,max_length=8192)
    enabled: bool | None = None
    requires_key: bool | None = None
    temperature: float | None = Field(default=None,ge=0,le=2)
    delete_key: bool = False

@router.get("/providers")
def all_profiles():
    with conn() as db: rows = db.execute("SELECT * FROM provider_profiles ORDER BY created_at,id").fetchall()
    return {"items": [providers.public_profile(row) for row in rows], "default_provider_id":providers.settings().get("default_provider_id","")}

def save_profile(body: ProfileInput, identifier: str | None = None):
    changes=body.model_dump(exclude_none=True,exclude_unset=True)
    is_new = identifier is None
    identifier = identifier or "provider_" + uid()
    if is_new:
        template=next((p for p in CATALOG if p["id"]==changes.get("catalog_id","custom")),None)
        if not template: raise HTTPException(422,"请选择有效厂商模板，或使用自定义服务。")
        current={k:template[k] for k in ("name","protocol","base_url","requires_key")}
        current.update(catalog_id=template["id"],model="",models=[],enabled=True,temperature=.5)
    else: current=providers.get_profile(identifier)
    secret=changes.pop("api_key", "").strip()
    delete_key=changes.pop("delete_key",False)
    if secret and delete_key: raise HTTPException(422,"不能同时保存与删除密钥。")
    current.update(changes)
    current["base_url"]=providers.canonical_url(current["base_url"])
    if any(len(x)>300 or not x.strip() for x in current.get("models",[])): raise HTTPException(422,"模型 ID 不能为空或超过 300 字符。")
    if secret and ("\n" in secret or "\r" in secret): raise HTTPException(422,"密钥格式不正确。")
    credential = providers.credential_name(current | {"provider_id": identifier})
    try:
        if secret: providers.keyring.set_password(providers.SERVICE,credential,secret)
        if delete_key and providers.api_key(current | {"provider_id":identifier},required=False): providers.keyring.delete_password(providers.SERVICE,credential)
    except Exception: raise HTTPException(503,"系统凭据库暂不可用，未保存本次服务配置。")
    current.pop("provider_id",None)
    with conn() as db:
        if secret: db.execute("INSERT OR IGNORE INTO provider_credential_bindings VALUES(?,?)",(identifier,credential))
        db.execute("INSERT INTO provider_profiles VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET config=excluded.config,updated_at=excluded.updated_at",(identifier,json.dumps(current,ensure_ascii=False),now(),now()))
        default=db.execute("SELECT value FROM settings WHERE key='default_provider_id'").fetchone()
        if not default or not json.loads(default[0]): db.execute("INSERT OR REPLACE INTO settings VALUES('default_provider_id',?)",(json.dumps(identifier),))
        row=db.execute("SELECT * FROM provider_profiles WHERE id=?",(identifier,)).fetchone()
    return providers.public_profile(row)

@router.post("/providers")
def create_profile(body: ProfileInput): return save_profile(body)

@router.put("/providers/{identifier}")
def update_profile(identifier: str,body: ProfileInput): return save_profile(body,identifier)

@router.post("/providers/{identifier}/default")
def default_profile(identifier: str):
    config=providers.get_profile(identifier)
    if not config.get("enabled"): raise HTTPException(409,"请先启用该服务。")
    with conn() as db: db.execute("INSERT OR REPLACE INTO settings VALUES('default_provider_id',?)",(json.dumps(identifier),))
    return {"ok":True,"default_provider_id":identifier}

@router.delete("/providers/{identifier}")
def delete_profile(identifier: str):
    config=providers.get_profile(identifier)
    with conn() as db: bindings={r[0] for r in db.execute("SELECT credential FROM provider_credential_bindings WHERE provider_id=?",(identifier,))}
    bindings.add(providers.credential_name(config))
    try:
        for binding in bindings:
            if providers.keyring.get_password(providers.SERVICE,binding): providers.keyring.delete_password(providers.SERVICE,binding)
    except Exception: raise HTTPException(503,"无法移除该服务凭据，请稍后重试。")
    with conn() as db:
        db.execute("DELETE FROM provider_profiles WHERE id=?",(identifier,))
        db.execute("DELETE FROM provider_credential_bindings WHERE provider_id=?",(identifier,))
        row=db.execute("SELECT value FROM settings WHERE key='default_provider_id'").fetchone()
        if row and json.loads(row[0])==identifier:
            db.execute("INSERT OR REPLACE INTO settings VALUES('default_provider_id','\"\"')")
            # Avoid silently falling back to a retired legacy endpoint/key.
            db.execute("INSERT OR REPLACE INTO settings VALUES('model','\"\"')")
    return {"ok":True,"notice":"服务及其当前凭据已移除；已有消息和群成员来源记录保留。"}

@router.get("/providers/{identifier}/models")
async def profile_models(identifier: str):
    config=providers.get_profile(identifier)
    if not config.get("enabled"): raise HTTPException(409,"该服务已停用。")
    return await providers.list_models(config)

class TestInput(BaseModel):
    model: str | None = Field(default=None,max_length=300)

@router.post("/providers/{identifier}/test")
async def test_profile(identifier: str,body: TestInput = TestInput()):
    config=providers.get_profile(identifier,body.model)
    result=await providers.complete([{"role":"user","content":"Reply OK only."}],max_tokens=512,config=config)
    return {"ok":True,"model":config["model"],"message":"所选模型生成测试成功。","response":result[:100]}
