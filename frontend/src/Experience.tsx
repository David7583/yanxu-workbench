import { useState } from 'react';
import { Check, ChevronDown, FolderOpen, Search, X } from 'lucide-react';
import { api, post, errorText } from './api';
import { Modal, Spinner } from './ui';
import type { Notify } from './types';

export function FlowSteps({steps,active=0,complete=false}:{steps:string[];active?:number;complete?:boolean}){
 return <ol className="flow-steps" aria-label="任务流程">{steps.map((step,i)=><li key={step} className={(complete||i<active?'done':i===active?'current':'waiting')}><span className="flow-node">{complete||i<active?<Check size={14}/>:i+1}</span><span>{step}</span>{i<steps.length-1&&<i className="flow-connector"/>}</li>)}</ol>;
}
type ModelOption = {id:string;name?:string;chat_supported?:boolean|null};
export function ModelPicker({value,onChange,models,label='模型',disabled=false}:{value:string;onChange:(v:string)=>void;models:(string|ModelOption)[];label?:string;disabled?:boolean}){
 const [open,setOpen]=useState(false),[query,setQuery]=useState(''),[showAll,setShowAll]=useState(false);
 const list:ModelOption[]=models.map(m=>typeof m==='string'?{id:m,name:m}:m).filter((m,i,a)=>a.findIndex(v=>v.id===m.id)===i);
 // Unknown capabilities remain selectable. The name hint only narrows the default view.
 const otherUse=(m:ModelOption)=>m.chat_supported===false||(m.chat_supported!==true&&/(?:^|[-_/])(?:embedding|embeddings|moderation|tts|whisper|transcribe|realtime|image|imagen|dall-e|sora|veo|rerank)(?:[-_/]|$)/i.test(m.id));
 const hidden=list.filter(m=>otherUse(m)&&m.id!==value).length;
 const filtered=list.filter(m=>(showAll||m.id===value||!otherUse(m))&&((m.name||'')+' '+m.id).toLowerCase().includes(query.toLowerCase()));
 return <div className="model-picker"><span className="field-label">{label}</span><button type="button" aria-label={label} aria-haspopup="dialog" aria-expanded={open} className="model-trigger" disabled={disabled} onClick={()=>{setQuery('');setShowAll(false);setOpen(true)}}><span>{value||'选择一个模型'}</span><ChevronDown size={16}/></button>{open&&<Modal title="选择模型" subtitle="搜索已获取的模型，或填写服务商提供的模型 ID。" onClose={()=>setOpen(false)}>
  <div className="model-search"><Search size={17}/><input autoFocus aria-label="搜索模型" placeholder="搜索模型名称…" value={query} onChange={e=>setQuery(e.target.value)}/></div>
  {hidden>0&&<div className="model-filter"><span className="muted">优先显示对话候选 · {filtered.length} 个</span><button type="button" className="text-button" aria-pressed={showAll} onClick={()=>setShowAll(!showAll)}>{showAll?'优先显示对话模型':`显示全部（含 ${hidden} 个其他用途候选）`}</button></div>}
  <div className="model-options">{filtered.map(m=><button key={m.id} className={value===m.id?'selected':''} onClick={()=>{onChange(m.id);setOpen(false)}}><span><b>{m.name||m.id}</b>{m.name&&m.name!==m.id&&<small>{m.id}</small>}{m.chat_supported===false&&<small>服务商标记为不支持对话</small>}</span>{value===m.id&&<Check size={17}/>}</button>)}{!filtered.length&&<p className="subtle">没有匹配的模型。可以显示完整列表、刷新服务商列表，或手动添加。</p>}</div>
  {hidden>0&&<p className="muted model-filter-hint">筛选参考服务商信息及模型名称，最终能力以厂商说明和实际调用为准。</p>}
  <div className="model-manual"><label>手动填写模型 ID<input aria-label="手动模型 ID" value={query} onChange={e=>setQuery(e.target.value)} placeholder="输入精确的模型 ID"/></label><button className="button secondary small" disabled={!query.trim()} onClick={()=>{onChange(query.trim());setOpen(false)}}>使用此模型</button></div></Modal>}</div>;
}
export function PathField({label,value,onChange,notify,placeholder}:{label:string;value:string;onChange:(v:string)=>void;notify:Notify;placeholder?:string}){
 const [busy,setBusy]=useState(false),[browser,setBrowser]=useState<any>(null),[typed,setTyped]=useState(value);
 const list=async(path:string)=>{try{const data=await api('/setup/folders?path='+encodeURIComponent(path));setBrowser(data);setTyped(data.path||path)}catch(e){notify(errorText(e),'error')}};
 const browse=async()=>{setBusy(true);try{const result=await post('/setup/folder-picker',{initial_dir:value});if(result.path)onChange(result.path);else if(result.cancelled!==true)await list(value)}catch{await list(value)}finally{setBusy(false)}};
 return <label className="path-field">{label}<span className="path-input"><input value={value} onChange={e=>onChange(e.target.value)} placeholder={placeholder||'选择或粘贴本机文件夹路径'}/><button type="button" className="button secondary small" disabled={busy} onClick={browse}>{busy?<Spinner label=""/>:<FolderOpen size={16}/>}浏览</button></span>{browser&&<Modal title="选择文件夹" onClose={()=>setBrowser(null)}><div className="path-input"><input aria-label="文件夹路径" value={typed} onChange={e=>setTyped(e.target.value)}/><button type="button" className="button secondary small" onClick={()=>list(typed)}>前往</button></div><div className="folder-list">{browser.parent&&<button type="button" onClick={()=>list(browser.parent)}>↑ 上一级</button>}{(browser.items||browser.directories||[]).map((d:any)=><button type="button" key={d.path||d} onClick={()=>list(d.path||d)}><FolderOpen size={16}/>{d.name||d.path||d}</button>)}</div><div className="modal-footer"><button type="button" className="button primary" onClick={()=>{onChange(browser.path||typed);setBrowser(null)}}>选择此文件夹</button></div></Modal>}</label>;
}
export function SourceDirectories({values,onChange,notify}:{values:string[];onChange:(v:string[])=>void;notify:Notify}){
 return <div className="source-directories">{values.map((path,i)=><div className="source-directory" key={i}><PathField label={'资料来源 '+(i+1)} value={path} onChange={v=>onChange(values.map((p,j)=>i===j?v:p))} notify={notify}/><button type="button" className="icon-button" aria-label={'移除资料来源 '+(i+1)} onClick={()=>onChange(values.filter((_,j)=>i!==j))}><X size={16}/></button></div>)}<button type="button" className="text-button" onClick={()=>onChange([...values,''])}>＋ 添加资料来源文件夹</button><small className="muted">保存来源后可以预览扫描结果，自行选择要导入的批次。</small></div>;
}
