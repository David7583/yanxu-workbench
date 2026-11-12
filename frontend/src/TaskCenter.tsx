import { useEffect, useRef, useState } from 'react';
import { Activity, ArrowUpRight, CheckCircle2, Clock3, FileText, FolderArchive, MessageSquare, Play, Square } from 'lucide-react';
import { api, post, items, errorText } from './api';
import { Modal, Spinner } from './ui';
import type { Notify } from './types';

export type WorkTask={id:string;kind:string;title?:string;status:string;progress:number;phase?:string;error?:string;project_id?:string;session_id?:string;manuscript_id?:string;can_resume?:boolean;resume_reason?:string;updated_at:string;created_at:string};
export const activeTask=(t:WorkTask)=>['queued','running','processing','cancelling'].includes(t.status);
export const statusLabel:Record<string,string>={queued:'排队中',running:'进行中',processing:'正在导入',cancelling:'正在暂停',completed:'已完成',done:'已完成',failed:'失败',error:'失败',cancelled:'已暂停',interrupted:'已中断',partial:'部分完成'};
const taskNames:Record<string,string>={import:'资料导入',discussion:'研究讨论',manuscript_diagnose:'原稿诊断',manuscript_proposal:'原稿改写',research_generate:'整理与稿件'};
export function useWorkQueue(){
 const [tasks,setTasks]=useState<WorkTask[]>([]),[error,setError]=useState('');const alive=useRef(true);
 const refresh=async()=>{try{const [feed,generated]=await Promise.all([api('/work-queue'),api('/tasks')]);if(alive.current){setTasks([...items<WorkTask>(feed),...items<WorkTask>(generated)].sort((a,b)=>b.updated_at.localeCompare(a.updated_at)));setError('')}}catch(e){if(alive.current)setError(errorText(e))}};
 useEffect(()=>{alive.current=true;void refresh();const timer=setInterval(()=>{void refresh()},3000);const changed=()=>{void refresh()};window.addEventListener('workbench-tasks',changed);return()=>{alive.current=false;clearInterval(timer);window.removeEventListener('workbench-tasks',changed)}},[]);
 return {tasks,error,refresh};
}
export function TaskRow({task,onOpen}:{task:WorkTask;onOpen:(t:WorkTask)=>void}){
 const busy=activeTask(task),Icon=task.kind==='import'?FolderArchive:task.kind==='discussion'?MessageSquare:FileText;
 return <button className={'task-row '+(busy?'active':'')} onClick={()=>onOpen(task)}><span className="task-symbol"><Icon size={18}/></span><span className="task-copy"><b>{task.title||taskNames[task.kind]||'研究任务'}</b><small>{taskNames[task.kind]} · {task.phase||statusLabel[task.status]||task.status}</small>{busy&&<progress aria-label="任务进度" max={100} value={task.progress>0?task.progress:undefined}/>}</span><span className="task-state">{busy?<span className="live-dot"/>:task.status==='completed'||task.status==='done'?<CheckCircle2 size={14}/>:<Clock3 size={14}/>} {statusLabel[task.status]||task.status}</span><ArrowUpRight size={15}/></button>;
}
export default function TaskCenter({tasks,error,refresh,onClose,onOpen,notify}:{tasks:WorkTask[];error:string;refresh:()=>Promise<void>;onClose:()=>void;onOpen:(t:WorkTask)=>void;notify:Notify}){
 const [filter,setFilter]=useState('all'),[busy,setBusy]=useState('');
 const act=async(t:WorkTask,action:string)=>{setBusy(t.id);try{await post(t.kind==='import'?`/imports/${t.id}/${action}`:t.kind==='discussion'?`/sessions/${t.session_id}/stop`:`/tasks/${t.id}/cancel`,{});await refresh();window.dispatchEvent(new Event('workbench-tasks'))}catch(e){notify(errorText(e),'error')}finally{setBusy('')}};
 const list=tasks.filter(t=>filter==='all'||(filter==='active'?activeTask(t):!activeTask(t)));
 return <Modal title="后台任务中心" subtitle="切换页面或关闭面板，任务仍在本机服务中继续。" onClose={onClose} wide><div className="task-center-top"><span><Activity size={18}/>{tasks.filter(activeTask).length} 个进行中</span><div className="pill-tabs">{[['all','全部'],['active','进行中'],['finished','已结束']].map(([key,label])=><button className={filter===key?'active':''} key={key} onClick={()=>setFilter(key)}>{label}</button>)}</div></div>{error&&<p className="notice error">{error}</p>}<div className="task-center-list">{!list.length?<p className="muted">这里会显示资料导入、群聊、论文诊断和稿件生成的进度。</p>:list.map(t=><article key={t.kind+t.id} className="task-detail"><TaskRow task={t} onOpen={onOpen}/>{t.error&&<p className="warning">{t.error}</p>}<div className="task-detail-actions"><span>{new Date(t.updated_at).toLocaleString('zh-CN')}</span>{busy===t.id?<Spinner/>:t.kind==='import'&&t.can_resume!==false&&['cancelled','interrupted','failed','error'].includes(t.status)?<button className="text-button" onClick={()=>act(t,'resume')}><Play size={13}/>继续导入</button>:activeTask(t)&&t.status!=='cancelling'?<button className="text-button" onClick={()=>act(t,t.kind==='import'?'cancel':'stop')}><Square size={12}/>{t.kind==='import'?'暂停':'停止任务'}</button>:null}<button className="text-button" onClick={()=>onOpen(t)}>查看{activeTask(t)?'进度':'结果'}<ArrowUpRight size={14}/></button></div></article>)}</div><p className="muted">后台运行依赖本机服务保持开启。服务停止后，未完成的 AI 任务会标记为中断，避免自动重复调用。</p></Modal>;
}
