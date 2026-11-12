export type Project = {id:string;title:string;description:string;discipline:string;created_at:string;updated_at:string;notes:string;draft_zh:string;draft_en:string;revision:number};
export type Chat = {id:string;role:string;content:string;created_at:string;sources:any[]};
export type Reference = {id:string;title:string;authors:any[];year:string|number;doi:string;url:string;abstract:string;publisher:string;type:string;verified:number};
export type Notify = (message:string, kind?:'success'|'error'|'info') => void;
export type Settings = {base_url:string;model:string;provider:string;has_key:boolean;export_dir:string;temperature:number;auto_research:boolean};
