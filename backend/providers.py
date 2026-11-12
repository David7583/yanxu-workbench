"""Provider configuration and bounded, credential-safe compatible API requests."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urlsplit, quote

import httpx
import keyring
from fastapi import HTTPException

from .core import ROOT, conn, json_load, uid, now

DEFAULTS = {
    "provider": "openai-compatible", "base_url": "https://api.openai.com/v1",
    "model": "", "export_dir": str(ROOT / "exports"), "temperature": 0.5,
    "auto_research": False, "auto_notes": False, "default_provider_id": "",
}
SERVICE = "paper-workbench-" + hashlib.sha256(str(ROOT).encode()).hexdigest()[:12]


def canonical_url(value: str) -> str:
    value = value.strip().rstrip("/")
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError:
        raise HTTPException(422, "服务地址格式不正确。")
    if (not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment
            or re.search(r"[\s\\]", value)):
        raise HTTPException(422, "服务地址需要完整 URL，不能包含账号、密码、查询参数或空白。")
    if parsed.scheme != "https" and not (
        parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    ):
        raise HTTPException(422, "在线服务须使用 HTTPS；本机服务可使用 HTTP。")
    return value


def settings() -> dict:
    result = DEFAULTS.copy()
    with conn() as db:
        for row in db.execute("SELECT key,value FROM settings"):
            if row["key"] in DEFAULTS:
                result[row["key"]] = json_load(row["value"], DEFAULTS[row["key"]])
    selected = result.get("default_provider_id")
    if selected:
        with conn() as db:
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='provider_profiles'").fetchone()
            row = db.execute("SELECT * FROM provider_profiles WHERE id=?", (selected,)).fetchone() if exists else None
        if row:
            profile = json_load(row["config"], {})
            result.update(profile, provider_id=row["id"])
    return result


def credential_name(config: dict) -> str:
    # A saved key is bound to the exact endpoint, so changing providers never forwards it.
    return hashlib.sha256((str(config.get("provider_id", "")) + ("|" if config.get("provider_id") else "") + canonical_url(config["base_url"])).encode()).hexdigest()


def api_key(config: dict, *, required: bool = True) -> str | None:
    try:
        value = keyring.get_password(SERVICE, credential_name(config))
    except Exception:
        if required:
            raise HTTPException(503, "系统凭据库暂不可用。请重新登录系统或检查凭据库设置。")
        return None
    if not value and required and config.get("requires_key", True):
        raise HTTPException(409, "请先在设置中为当前服务保存 API key。密钥只保存在系统凭据库。")
    return value


def public_settings() -> dict:
    config = settings()
    config["has_key"] = bool(api_key(config, required=False)) or not config.get("requires_key", True)
    config["credential_storage"] = "system-keyring"
    config["supported_provider"] = config.get("protocol", "OpenAI-compatible Chat Completions")
    return config


def save_settings(changes: dict) -> dict:
    current = settings()
    updates = {key: value for key, value in changes.items() if key in DEFAULTS}
    if "base_url" in updates:
        updates["base_url"] = canonical_url(updates["base_url"])
    if "export_dir" in updates:
        location = Path(updates["export_dir"].strip() or DEFAULTS["export_dir"]).expanduser()
        if not location.is_absolute():
            raise HTTPException(422, "导出目录必须是绝对路径。")
        updates["export_dir"] = str(location.resolve())
    if "provider" in updates and updates["provider"] not in {"openai-compatible", "anthropic", "gemini", "openai-responses"}:
        raise HTTPException(422, "当前版本支持 OpenAI-compatible 协议；请填写对应兼容服务地址。")
    updated = current | updates
    secret = changes.get("api_key", "").strip()
    if secret and (len(secret) > 8192 or re.search(r"[\r\n]", secret)):
        raise HTTPException(422, "API key 格式不正确。")
    if secret and changes.get("delete_key"):
        raise HTTPException(422, "不能同时保存与删除密钥。")
    try:
        if secret:
            keyring.set_password(SERVICE, credential_name(updated), secret)
        elif changes.get("delete_key"):
            if api_key(updated, required=False):
                keyring.delete_password(SERVICE, credential_name(updated))
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(503, "无法写入系统凭据库，设置未保存；没有将密钥写入普通文件。")
    with conn() as db:
        for key, value in updates.items():
            db.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                       (key, json.dumps(value, ensure_ascii=False)))
    if current.get("provider_id") and any(k in updates for k in ("model", "base_url", "temperature")):
        profile = get_profile(current["provider_id"])
        profile.update({k: updates[k] for k in ("model", "base_url", "temperature") if k in updates})
        with conn() as db:
            db.execute("UPDATE provider_profiles SET config=?,updated_at=? WHERE id=?", (json.dumps(profile, ensure_ascii=False), now(), current["provider_id"]))
    return public_settings()


async def request_provider(config: dict, method: str, endpoint: str, payload: dict | None = None) -> dict:
    base = canonical_url(config["base_url"])
    secret = api_key(config)
    protocol = config.get("protocol", "openai-compatible")
    headers = {"Content-Type": "application/json"}
    if secret:
        if protocol == "anthropic":
            headers.update({"x-api-key": secret, "anthropic-version": "2023-06-01"})
        elif protocol == "gemini":
            headers["x-goog-api-key"] = secret
        else:
            headers["Authorization"] = "Bearer " + secret
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(120, connect=15), follow_redirects=False) as client:
            response = await client.request(method, base + endpoint, headers=headers, json=payload)
        if response.status_code in {401, 403}:
            raise HTTPException(502, "模型服务拒绝了认证，请检查当前服务的 API key 与访问权限。")
        if response.status_code == 429:
            raise HTTPException(429, "模型服务限流或余额不足，请检查账户后重试。")
        if not 200 <= response.status_code < 300:
            raise HTTPException(502, f"模型服务返回 HTTP {response.status_code}，请检查服务地址、模型名与兼容协议。")
        data = response.json()
        if not isinstance(data, dict):
            raise HTTPException(502, "模型服务返回了不支持的数据结构。")
        return data
    except HTTPException:
        raise
    except httpx.TimeoutException:
        raise HTTPException(504, "模型请求超时；未改动稿件。可以稍后重试。")
    except (httpx.HTTPError, ValueError):
        # Do not expose upstream bodies/URLs: some gateways echo credentials in errors.
        raise HTTPException(502, "无法连接模型服务，或服务返回了非 JSON 数据。请检查网络与地址。")


async def complete(messages: list[dict], *, max_tokens: int = 4096, config: dict | None = None) -> str:
    config = dict(config or settings())
    if config.get("enabled") is False:
        raise HTTPException(409, "该 AI 服务已停用，请选择其他服务。")
    model = config.get("model", "").strip()
    if not model:
        raise HTTPException(409, "请先选择一个模型。")
    protocol = config.get("protocol", "openai-compatible")
    system = "\n".join(str(m["content"]) for m in messages if m["role"] == "system")
    turns = [m for m in messages if m["role"] != "system"]
    if protocol == "anthropic":
        payload = {"model": model, "system": system, "messages": turns, "max_tokens": max_tokens}
        data = await request_provider(config, "POST", "/messages", payload)
        if data.get("stop_reason") == "max_tokens":
            raise HTTPException(502, "模型输出达到长度上限；请缩小生成范围。")
        content = "\n".join(x.get("text", "") for x in data.get("content", []) if x.get("type") == "text")
    elif protocol == "gemini":
        payload = {"contents": [{"role": "model" if m["role"] == "assistant" else "user", "parts": [{"text": m["content"]}]} for m in turns],
                   "generationConfig": {"maxOutputTokens": max_tokens}}
        if system: payload["systemInstruction"] = {"parts": [{"text": system}]}
        data = await request_provider(config, "POST", "/models/" + quote(model.removeprefix("models/"), safe="-._") + ":generateContent", payload)
        candidates = data.get("candidates") or []
        if not candidates: raise HTTPException(502, "模型未返回可显示的答案；请检查内容限制或模型能力。")
        if candidates[0].get("finishReason") == "MAX_TOKENS": raise HTTPException(502, "模型输出达到长度上限；请缩小生成范围。")
        content = "\n".join(x.get("text", "") for x in candidates[0].get("content", {}).get("parts", []) if not x.get("thought"))
    elif protocol == "openai-responses":
        payload = {"model": model, "input": messages, "max_output_tokens": max_tokens, "store": False}
        data = await request_provider(config, "POST", "/responses", payload)
        if data.get("status") in {"incomplete", "failed"}: raise HTTPException(502, "模型未完成本次生成，请缩小范围或稍后重试。")
        content = "\n".join(part.get("text", "") for item in data.get("output", []) if item.get("type") == "message" for part in item.get("content", []) if part.get("type") == "output_text")
    else:
        payload = {"model": model, "messages": messages, "stream": False}
        if re.match(r"^(o[1-9]|gpt-[5-9])", model):
            payload["max_completion_tokens"] = max_tokens
        else:
            payload.update(temperature=config.get("temperature", .5), max_tokens=max_tokens)
        data = await request_provider(config, "POST", "/chat/completions", payload)
        try:
            choice = data["choices"][0]
            if choice.get("finish_reason") == "length": raise HTTPException(502, "模型输出达到长度上限；请缩小生成范围。")
            content = choice["message"]["content"]
            if isinstance(content, list): content = "\n".join(part.get("text", "") for part in content if isinstance(part, dict))
        except (KeyError, IndexError, TypeError):
            raise HTTPException(502, "模型没有返回有效文字，请检查模型与接口协议。")
    if not isinstance(content, str) or not content.strip(): raise HTTPException(502, "模型没有返回有效文字，请检查模型能力。")
    if isinstance(config.get("_telemetry"), dict):
        config["_telemetry"].update(response_model=data.get("model") or data.get("modelVersion") or "", usage=data.get("usage") or data.get("usageMetadata") or {})
    return content.strip()


def init_schema():
    with conn() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS provider_profiles(id TEXT PRIMARY KEY, config TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS provider_catalog_hidden(id TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS provider_credential_bindings(provider_id TEXT NOT NULL, credential TEXT NOT NULL, PRIMARY KEY(provider_id,credential));
        """)
        migrated = db.execute("SELECT value FROM settings WHERE key='providers_migrated'").fetchone()
    if not migrated:
        old = settings()
        secret = api_key(old, required=False)
        if secret or old.get("model"):
            identifier = "provider_" + uid()
            cfg = {"name": "原有 AI 服务", "catalog_id": "custom", "protocol": "openai-compatible", "base_url": old["base_url"], "model": old.get("model", ""), "models": [], "enabled": True, "requires_key": True, "temperature": old.get("temperature", .5)}
            if secret:
                try: keyring.set_password(SERVICE, credential_name(cfg | {"provider_id": identifier}), secret)
                except Exception: raise HTTPException(503, "迁移凭据暂时失败；原有凭据保留，请重试。")
            with conn() as db:
                db.execute("INSERT INTO provider_profiles VALUES(?,?,?,?)", (identifier, json.dumps(cfg, ensure_ascii=False), now(), now()))
                if secret:
                    db.execute("INSERT OR IGNORE INTO provider_credential_bindings VALUES(?,?)",(identifier,credential_name(old)))
                    db.execute("INSERT OR IGNORE INTO provider_credential_bindings VALUES(?,?)",(identifier,credential_name(cfg|{"provider_id":identifier})))
                db.execute("INSERT OR REPLACE INTO settings VALUES('default_provider_id',?)", (json.dumps(identifier),))
        with conn() as db: db.execute("INSERT OR REPLACE INTO settings VALUES('providers_migrated','true')")


def get_profile(identifier: str, model: str | None = None) -> dict:
    with conn() as db: row = db.execute("SELECT * FROM provider_profiles WHERE id=?", (identifier,)).fetchone()
    if not row: raise HTTPException(404, "AI 服务配置不存在。")
    config = json_load(row["config"], {}) | {"provider_id": identifier}
    if model is not None: config["model"] = model
    return config


def public_profile(row) -> dict:
    value = json_load(row["config"], {})
    value.update(id=row["id"], created_at=row["created_at"], updated_at=row["updated_at"])
    value.pop("provider_id", None)
    value["has_key"] = bool(api_key(value | {"provider_id": row["id"]}, required=False))
    value["is_default"] = settings().get("default_provider_id") == row["id"]
    return value


async def list_models(config: dict) -> dict:
    protocol = config.get("protocol", "openai-compatible")
    collected, cursor = [], ""
    for page in range(10):
        endpoint = "/models"
        if protocol == "gemini": endpoint += "?pageSize=100" + ("&pageToken=" + quote(cursor, safe="") if cursor else "")
        elif protocol == "anthropic": endpoint += "?limit=100" + ("&after_id=" + quote(cursor, safe="") if cursor else "")
        data = await request_provider(config, "GET", endpoint)
        for item in data.get("models" if protocol == "gemini" else "data", []):
            if not isinstance(item, dict): continue
            identifier = str(item.get("name" if protocol == "gemini" else "id", "")).removeprefix("models/")
            if not identifier: continue
            methods = item.get("supportedGenerationMethods", [])
            chat = "generateContent" in methods if protocol == "gemini" else None
            collected.append({"id": identifier, "name": item.get("displayName") or item.get("display_name") or identifier,
                              "capabilities": item.get("capabilities") or {}, "chat_supported": chat})
        cursor = data.get("nextPageToken", "") if protocol == "gemini" else data.get("last_id", "") if protocol == "anthropic" and data.get("has_more") else ""
        if not cursor: break
    return {"items": sorted(collected, key=lambda item: item["id"]), "notice": "模型列表来自当前服务。列表可见不代表已验证推理权限；可单独测试所选模型。"}
