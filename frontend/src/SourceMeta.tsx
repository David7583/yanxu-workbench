import { useState } from 'react';
import { History, FileText } from 'lucide-react';
import { api, items, errorText } from './api';
import type { Notify } from './types';
export default function SourceMeta({message,sourceId,notify}:{message:any;sourceId:string;notify:Notify}){
 const [versions,setVersions]=useState<any[]|null>(null),[expanded,setExpanded]=useState(false),[busy,setBusy]=useState(false);
 const m=message,meta=typeof m.metadata==='string'?{}:m.metadata||{};
 const time=m.created_at?new Date(m.created_at).toLocaleString('zh-CN',{timeZoneName:'short'}):'发言时间未提供';
 return <div className="source-meta"><div className="archive-message-meta"><b>{m.author_name||(m.role==='user'?'你':m.role==='assistant'?'AI':m.role==='thinking'?'思考片段':m.role==='tool'?'工具结果':m.role)}</b><span className="tag light">{m.model||'模型未提供'}</span>{m.branch&&m.branch!=='main'&&<span className="tag">替代分支</span>}<span>{time}</span></div><details className="source-trace"><summary>来源与版本</summary><dl><dt>原始时间值</dt><dd>{m.original_time||'导出中未提供'}</dd><dt>原始消息标识</dt><dd>{meta.external_id||m.id}</dd><dt>导入时间</dt><dd>{meta.imported_at||'未记录'}</dd><dt>来源批次</dt><dd>{typeof meta.origin==='object'?JSON.stringify(meta.origin):meta.origin||'查看所属对话的原始文件'}</dd>{meta.conversation_model&&<><dt>对话级模型记录</dt><dd>{meta.conversation_model}（不代替每条消息的模型）</dd></>}</dl><button className="text-button" disabled={busy} onClick={async()=>{setBusy(true);try{setVersions(items(await api('/sources/'+sourceId+'/messages/'+m.id+'/versions')));setExpanded(!expanded)}catch(e){notify(errorText(e),'error')}finally{setBusy(false)}}}><History size={12}/>查看历史版本</button>{expanded&&<div className="source-versions">{!versions?.length?<p>尚无其他内容版本。</p>:versions.map(v=><div key={v.id}><small>{v.edited_at||v.created_at||'时间未提供'} · {v.model||'模型未提供'}</small><pre>{v.content}</pre></div>)}</div>}</details></div>;
}
