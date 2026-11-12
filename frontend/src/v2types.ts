export type Provider = {id:string;name:string;catalog_id:string;protocol:string;base_url:string;model:string;enabled:boolean;has_key:boolean;is_default:boolean;models:string[];requires_key?:boolean};
export type CatalogItem = {id:string;name:string;protocol:string;base_url:string;docs_url:string;requires_key:boolean;region?:string;hidden?:boolean};
export type Setup = {completed:boolean;source_dirs:string[];data_dir:string;export_dir:string;language:string;citation_style:string;pending_restart?:boolean};
export type Member = {id:string;name:string;provider_id:string;model:string;role:string;agent_id?:string;enabled:boolean};
export type Session = {id:string;title:string;kind:'direct'|'group';status:string};
export const roles:Record<string,string>={balanced:'研究伙伴',critical:'批判性审阅',writing:'写作协调',explore:'选题探索'};
