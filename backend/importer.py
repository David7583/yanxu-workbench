"""Streaming adapters for conversation exports. Raw input is never rewritten."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

from bs4 import BeautifulSoup
import ijson

from .core import conn, json_load, now

POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="paper-import")
CANCELLED: dict[str, threading.Event] = {}
JOB_LOCK = threading.RLock()
WORKERS: set[str] = set()


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def stamp(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, dict) and "$date" in value:
        date=value["$date"]
        if isinstance(date,dict) and "$numberLong" in date:
            try:return datetime.fromtimestamp(int(date["$numberLong"])/1000,timezone.utc).isoformat()
            except (ValueError,TypeError,OverflowError,OSError):return None
        return stamp(date)
    if isinstance(value, str):
        return value
    try:
        return datetime.fromtimestamp(float(value), timezone.utc).isoformat()
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def content_text(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(filter(None, (content_text(part) for part in value)))
    if isinstance(value, dict):
        if "parts" in value:
            return content_text(value["parts"])
        for key in ("text", "content", "value"):
            if key in value:
                result = content_text(value[key])
                if result:
                    return result
        if value.get("asset_pointer"):
            return f"[附件引用：{value['asset_pointer']}；附件内容未解析]"
        if value.get("type") in ("image", "image_url", "file", "document", "tool_use", "tool_result"):
            return f"[{value['type']} 内容；原始记录中保留，未作为正文解析]"
    return ""


def role_name(role) -> str:
    role = str(role or "unknown").lower().strip()
    return {"human": "user", "用户": "user", "你": "user", "ai": "assistant",
            "gpt": "assistant", "chatgpt": "assistant", "助手": "assistant", "claude": "assistant",
            "gemini": "assistant", "deepseek": "assistant", "model": "assistant"}.get(role, role)


def normalize_conversation(raw: dict, filename: str, ordinal: int = 0) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("JSON 对话项必须是对象")
    if isinstance(raw.get("conversation"), dict) and isinstance(raw.get("responses"), list):
        raw = {**raw["conversation"], "provider": "Grok", "messages": [x.get("response", x) for x in raw["responses"]]}
    title = str(raw.get("title") or raw.get("name") or Path(filename).stem or "未命名对话")[:500]
    external_id = raw.get("conversation_id") or raw.get("uuid") or raw.get("id")
    provider = str(raw.get("provider") or raw.get("source") or "通用导出")[:100]
    messages, warnings = [], []
    source_model = raw.get("model") or raw.get("default_model_slug")
    def append(msg, index, **extra):
        text = extra.pop("content", content_text(msg.get("content", msg.get("text", msg.get("message", "")))))
        if not text.strip(): return
        author = msg.get("author") or {}
        metadata = msg.get("metadata") or {}
        if not isinstance(metadata, dict): metadata = {}
        original_time = msg.get("created_at") or msg.get("create_time") or msg.get("timestamp") or msg.get("inserted_at")
        model = msg.get("model") or metadata.get("model_slug") or metadata.get("model")
        messages.append({"external_id": str(msg.get("uuid") or msg.get("id") or msg.get("_id") or ""),
            "role": role_name(msg.get("role") or msg.get("sender") or (author.get("role") if isinstance(author, dict) else author)),
            "content": text, "created_at": stamp(original_time), "original_time": str(original_time) if original_time is not None else None,
            "edited_at": stamp(msg.get("updated_at") or msg.get("update_time")),
            "author_name": msg.get("sender_name") or (author.get("name") if isinstance(author, dict) else None),
            "model": str(model) if model else None,
            "metadata": {"model_scope": "message" if model else "not_provided", "conversation_model": source_model,
                "attachments": msg.get("attachments") or msg.get("files") or [], "original_role": msg.get("role") or msg.get("sender"),
                "channel": msg.get("channel")},
            "parent_id": msg.get("parent_message_uuid") or msg.get("parent_id"), "branch": str(msg.get("branch") or "main"), "ordinal": index, **extra})
    mapping = raw.get("mapping")
    if isinstance(mapping, dict):
        deepseek = any(isinstance(n,dict) and isinstance(n.get("message"),dict) and "fragments" in n["message"] for n in mapping.values())
        provider = "DeepSeek" if deepseek else "ChatGPT"
        current, selected = raw.get("current_node"), set()
        node = current
        while node in mapping and node not in selected:
            selected.add(node); node = (mapping.get(node) or {}).get("parent")
        for index, (node_id, node_data) in enumerate(mapping.items()):
            if not isinstance(node_data, dict) or not isinstance(node_data.get("message"), dict): continue
            msg = node_data["message"]
            extra = {"external_id": str(msg.get("id") or node_id), "node_id": str(node_id), "parent_id": node_data.get("parent"),
                "branch": "main" if node_id in selected else ("alternative" if current else "unresolved")}
            if deepseek:
                parent_node=mapping.get(node_data.get("parent")) or {}
                parent_fragments=(parent_node.get("message") or {}).get("fragments",[])
                extra["parent_id"]=f"{node_data['parent']}:{len(parent_fragments)-1}" if parent_fragments else None
                for fi,fragment in enumerate(msg.get("fragments", [])):
                    kind = fragment.get("type", "UNKNOWN")
                    text = content_text(fragment.get("content"))
                    if not text and kind == "FILE": text = "[附件信息保留；正文未解析]"
                    if not text and kind.startswith("TOOL"): text = json.dumps(fragment.get("results", []), ensure_ascii=False, default=str)
                    append({**msg, "role": "user" if kind == "REQUEST" else "assistant" if kind in ("RESPONSE", "THINK") else "tool", "channel": kind}, index,
                        **{**extra, "external_id": f"{node_id}:{fi}", "node_id": f"{node_id}:{fi}", "parent_id":f"{node_id}:{fi-1}" if fi else extra["parent_id"]}, content=text)
            else: append(msg, index, **extra)
        messages.sort(key=lambda msg: (msg["created_at"] or "", msg["ordinal"]))
        if not current: warnings.append("部分对话没有 current_node，已保留全部分支，未推断最终采纳路径。")
    else:
        raw_messages = raw.get("chat_messages", raw.get("messages"))
        if "chat_messages" in raw: provider = "Claude"
        if not isinstance(raw_messages, list): raise ValueError("未识别的对话结构；请使用原始聊天导出，账户或统计文件不作为对话导入")
        for index, msg in enumerate(raw_messages):
            if not isinstance(msg, dict):
                warnings.append("跳过非对象消息；原始文件保留。"); continue
            if "user" in msg or "assistant" in msg:
                role = "user" if "user" in msg else "assistant"; msg = {"role":role,"content":msg[role]}
            if msg.get("attachments") or msg.get("files"): warnings.append("附件元数据已保留，附件正文尚未解析。")
            append(msg, index)
    for index, msg in enumerate(messages):
        msg["ordinal"] = index
        msg["fingerprint"] = digest(msg["role"] + "\0" + msg["content"].strip())
    if not messages: warnings.append("没有可解析的文本消息。")
    content_hash = digest("\n".join(msg["fingerprint"] for msg in messages))
    state_hash = digest(json.dumps([(m["fingerprint"],m["branch"],m["parent_id"],m["ordinal"],m.get("model")) for m in messages],ensure_ascii=False))
    source_id = digest(f"{provider}:{external_id}")[:32] if external_id else digest(f"content:{provider}:{content_hash}")[:32]
    return {"id":source_id,"title":title,"provider":provider,"kind":"conversation",
        "created_at":stamp(raw.get("create_time") or raw.get("created_at") or raw.get("inserted_at")) or "",
        "messages":messages,"metadata":{"external_id":external_id,"content_hash":content_hash,"state_hash":state_hash,
            "warnings":list(dict.fromkeys(warnings)),"current_node":raw.get("current_node"),
            "updated_at":stamp(raw.get("update_time") or raw.get("updated_at") or raw.get("modify_time")),
            "conversation_model":source_model,"imported_at":now(),"schema_version":2}}


def iter_json_stream(stream, filename: str):
    first = stream.read(4096).lstrip(b"\xef\xbb\xbf \r\n\t")[:1]
    stream.seek(0)
    if stream.read(3) != b"\xef\xbb\xbf": stream.seek(0)
    start = stream.tell()
    if first == b"[":
        prefix = "item"
        # Some exports represent one conversation as [{user:...},{assistant:...}].
        sample = next(ijson.items(stream, "item"), None); stream.seek(start)
        if isinstance(sample,dict) and set(sample).issubset({"user","assistant"}) and sample:
            value = list(ijson.items(stream,"item"))
            yield normalize_conversation({"provider":"千问","messages":value},filename),stream.tell(); return
    elif first == b"{":
        prefix = ""
        for index,(key_path,event,value) in enumerate(ijson.parse(stream)):
            if key_path == "" and event == "map_key":
                if value in ("conversations","chats","items"): prefix = value + ".item"; break
                if value in ("mapping","messages","chat_messages"): break
            if index > 1000: break
        stream.seek(start)
    else: raise ValueError("JSON 文件必须包含对话数组或对象")
    found=False
    for index,value in enumerate(ijson.items(stream,prefix)):
        found=True; yield normalize_conversation(value,filename,index),stream.tell()
    if not found: raise ValueError("JSON 中没有找到可导入的对话")


def iter_json(path: Path, filename: str):
    with path.open("rb") as stream:
        yield from iter_json_stream(stream,filename)


ROLE_HEADER = re.compile(r"(?im)^\s*(?:#{1,6}\s+|\*\*)?(user|assistant|human|ai|chatgpt|gpt|claude|gemini|deepseek|用户|助手|你)\s*(?:\*\*)?\s*[:：]?\s*(?:\*\*)?\s*$")


def iter_markdown(path: Path, filename: str):
    # Markdown/HTML exports are individual documents; keep an explicit cap so a
    # giant non-streamable document cannot exhaust the local process.
    if path.stat().st_size > 64 * 1024 * 1024:
        raise ValueError("单份 Markdown/HTML 超过 64 MB，请拆分；大型聊天档案请使用 JSON 导入。")
    text = path.read_text(encoding="utf-8-sig")
    matches = list(ROLE_HEADER.finditer(text))
    messages = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[match.end():end].strip()
        if body:
            messages.append({"role": role_name(match.group(1)), "content": body})
    if not messages:
        messages = [{"role": "document", "content": text}]
    title_match = re.search(r"(?m)^#\s+(.+)$", text)
    result = normalize_conversation({"title": title_match.group(1) if title_match else Path(filename).stem,
                                   "provider": "Markdown", "messages": messages}, filename)
    if not matches:
        result["kind"] = "document"
        result["metadata"]["warnings"].append("未找到明确角色标题，按原始文档保存，未猜测发言者。")
    yield result, path.stat().st_size


def iter_html(path: Path, filename: str):
    if path.stat().st_size > 64 * 1024 * 1024:
        raise ValueError("单份 HTML 超过 64 MB，请拆分或使用原始 JSON。")
    soup = BeautifulSoup(path.read_text(encoding="utf-8-sig"), "html.parser")
    for element in soup(["script", "style", "noscript", "iframe", "object"]):
        element.decompose()
    activity = soup.select(".outer-cell") if "gemini" in filename.lower() or "Gemini Apps" in soup.get_text(" ",strip=True)[:500] else []
    if activity:
        for index,cell in enumerate(activity):
            text=cell.get_text("\n",strip=True)
            record=normalize_conversation({"provider":"Gemini","messages":[{"role":"document","content":text}]},filename,index)
            record["title"]="Gemini 活动记录 · "+str(index+1)
            record["kind"]="activity"
            record["metadata"]["warnings"].append("活动记录未必包含完整问答；没有推断缺失回复、发言者或时间。")
            yield record,path.stat().st_size
        return
    groups = soup.select(".conversation") or [soup]
    for group in groups:
        heading = group.find(["h1", "h2", "title"])
        title = heading.get_text(" ", strip=True) if heading else Path(filename).stem
        nodes = group.select("[data-message-author-role]") or group.select(".message")
        messages = []
        for element in nodes:
            author = element.get("data-message-author-role")
            if not author:
                label = element.find(["h3", "h4", "strong"])
                author = label.get_text(strip=True).rstrip(":：") if label else "unknown"
            text = element.get_text("\n", strip=True)
            if text:
                messages.append({"role": role_name(author), "content": text})
        if not messages:
            messages = [{"role": "document", "content": group.get_text("\n", strip=True)}]
        result = normalize_conversation({"title": title, "provider": "HTML", "messages": messages}, filename)
        if messages[0]["role"] == "document":
            result["kind"] = "document"
            result["metadata"]["warnings"].append("未识别消息边界，保存为纯文本原档；未执行网页脚本。")
        yield result, path.stat().st_size


def store_conversation(record: dict, source_file: str, original_name: str) -> bool:
    """Merge stable IDs; keep message revisions and never replace a newer dated export."""
    with conn() as db:
        db.execute("BEGIN IMMEDIATE")
        existing = db.execute("SELECT * FROM sources WHERE id=?",(record["id"],)).fetchone()
        cross_format_duplicate=False
        if not existing and record["messages"]:
            existing=db.execute("SELECT * FROM sources WHERE json_extract(metadata,'$.content_hash')=? AND (provider IN ('HTML','Markdown') OR ? IN ('HTML','Markdown') OR (provider=? AND json_extract(metadata,'$.external_id') IS NULL AND ? IS NULL)) LIMIT 1",(record["metadata"]["content_hash"],record["provider"],record["provider"],record["metadata"].get("external_id"))).fetchone()
            if existing: record["id"]=existing["id"];cross_format_duplicate=True
        metadata=json_load(existing["metadata"],{}) if existing else {}
        old_state=metadata.get("state_hash")
        def time_value(value):
            try:return datetime.fromisoformat(str(value).replace("Z","+00:00")).timestamp()
            except (ValueError,TypeError):return None
        old_time=time_value(metadata.get("updated_at")); new_time=time_value(record["metadata"].get("updated_at"))
        stale=old_time is not None and new_time is not None and new_time<old_time
        origins=metadata.get("origins",[])
        origin={"filename":original_name,"path":source_file}
        if origin not in origins:origins.append(origin)
        if not cross_format_duplicate and not stale:metadata.update(record["metadata"])
        metadata["origins"]=origins
        if existing:
            db.execute("UPDATE sources SET metadata=? WHERE id=?",(json.dumps(metadata,ensure_ascii=False,default=str),record["id"]))
            if cross_format_duplicate or old_state==record["metadata"]["state_hash"]:return False
        else:
            db.execute("INSERT INTO sources VALUES (?,?,?,?,?,?,?,?)",(record["id"],record["title"],record["provider"],record["kind"],record["created_at"],0,source_file,json.dumps(metadata,ensure_ascii=False,default=str)))
        changed=not bool(existing)
        for msg in record["messages"]:
            identifier=msg.get("node_id") or msg.get("external_id") or f"{msg['ordinal']}:{msg['fingerprint']}"
            message_id=digest(record["id"]+":"+identifier)[:32]
            parent_id=digest(record["id"]+":"+str(msg["parent_id"]))[:32] if msg["parent_id"] else None
            previous=db.execute("SELECT * FROM source_messages WHERE id=?",(message_id,)).fetchone()
            if previous and previous["content"]==msg["content"] and previous["role"]==msg["role"] and previous["branch"]==msg["branch"] and previous["model"]==msg.get("model"):continue
            if previous:
                # Record both the old and incoming variant, including provenance.
                for variant,origin_path in [(dict(previous),existing["source_file"] if existing else source_file),(msg,source_file)]:
                    version_id=digest(message_id+":"+variant["content"]+":"+str(variant.get("edited_at"))+":"+origin_path)[:32]
                    db.execute("INSERT OR IGNORE INTO source_message_versions VALUES (?,?,?,?,?,?,?,?,?,?)",(version_id,message_id,variant["content"],variant["role"],variant.get("model"),variant.get("created_at"),variant.get("edited_at"),now(),origin_path,json.dumps(variant.get("metadata",{}),ensure_ascii=False,default=str)))
                prior_edit=time_value(previous["edited_at"]); incoming_edit=time_value(msg.get("edited_at"))
                if stale or (prior_edit is not None and incoming_edit is not None and incoming_edit<prior_edit):continue
            msg_meta={**msg.get("metadata",{}),"external_id":msg.get("external_id"),"origin":origin,"imported_at":now()}
            db.execute("""INSERT INTO source_messages(id,source_id,role,content,created_at,parent_id,branch,ordinal,fingerprint,model,author_name,original_time,edited_at,metadata)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET content=excluded.content,role=excluded.role,branch=excluded.branch,parent_id=excluded.parent_id,ordinal=excluded.ordinal,fingerprint=excluded.fingerprint,created_at=excluded.created_at,model=excluded.model,author_name=excluded.author_name,original_time=excluded.original_time,edited_at=excluded.edited_at,metadata=excluded.metadata""",
                (message_id,record["id"],msg["role"],msg["content"],msg["created_at"],parent_id,msg["branch"],msg["ordinal"],msg["fingerprint"],msg.get("model"),msg.get("author_name"),msg.get("original_time"),msg.get("edited_at"),json.dumps(msg_meta,ensure_ascii=False,default=str)))
            try:
                db.execute("DELETE FROM messages_fts WHERE message_id=?",(message_id,))
                db.execute("INSERT INTO messages_fts(message_id,title,content) VALUES (?,?,?)",(message_id,record["title"],msg["content"]))
            except sqlite3.OperationalError:pass
            changed=True
        db.execute("UPDATE sources SET message_count=(SELECT count(*) FROM source_messages WHERE source_id=?) WHERE id=?",(record["id"],record["id"]))
        return changed


def process_import(job_id: str, path: Path, filename: str):
    """Uploaded files use the same resumable runner as directory and ZIP imports."""
    from .archive_routes import process_archive
    with conn() as db:
        row=db.execute("SELECT * FROM imports WHERE id=?",(job_id,)).fetchone()
        if not row:return
        stat=path.stat()
        db.execute("UPDATE imports SET source_path=COALESCE(source_path,?),scan_snapshot=COALESCE(scan_snapshot,?) WHERE id=?",(str(path),json.dumps({"bytes":stat.st_size,"mtime_ns":stat.st_mtime_ns}),job_id))
    process_archive(job_id)


def start_import(job_id: str, path: Path, filename: str):
    with JOB_LOCK:CANCELLED.setdefault(job_id,threading.Event())
    POOL.submit(process_import, job_id, path, filename)
