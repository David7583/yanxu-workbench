import { useEffect, useRef, type ReactNode } from 'react';
import { X, LoaderCircle, ArrowUpRight, Inbox } from 'lucide-react';
export function Spinner({label='处理中'}:{label?:string}) {return <span className="busy"><LoaderCircle className="spin" size={15}/>{label}</span>}
export function Empty({title,description,action,icon}:{title:string;description:string;action?:ReactNode;icon?:ReactNode}) {return <div className="empty-state"><div className="empty-icon">{icon||<Inbox size={27}/>}</div><h3>{title}</h3><p>{description}</p>{action}</div>}
export function Modal({title,subtitle,children,onClose,wide=false}:{title:string;subtitle?:string;children:ReactNode;onClose:()=>void;wide?:boolean}) {
 const ref=useRef<HTMLDialogElement>(null);
 useEffect(()=>{const d=ref.current;d?.showModal();return()=>{d?.close()}},[]);
 return <dialog ref={ref} className={'modal '+(wide?'wide':'')} onCancel={e=>{e.preventDefault();onClose()}} onClick={e=>{if(e.target===ref.current)onClose()}}><div className="modal-card"><div className="modal-head"><div><h2>{title}</h2>{subtitle&&<p>{subtitle}</p>}</div><button className="icon-button" aria-label="关闭窗口" onClick={onClose}><X size={20}/></button></div>{children}</div></dialog>
}
export function PageHeading({eyebrow,title,description,action}:{eyebrow:string;title:string;description:string;action?:ReactNode}) {return <div className="page-heading"><div><span className="eyebrow">{eyebrow}</span><h1>{title}</h1><p>{description}</p></div>{action}</div>}
export function External({href,children}:{href:string;children:ReactNode}) {return /^https?:\/\//i.test(href)?<a href={href} target="_blank" rel="noopener noreferrer" className="external-link">{children}<ArrowUpRight size={14}/></a>:<span>{children}</span>}
export const dateLabel=(date:string)=>!date?'时间未提供':new Date(date.endsWith('Z')||/[+-]\d\d:\d\d$/.test(date)?date:date+'Z').toLocaleDateString('zh-CN',{month:'short',day:'numeric'});
