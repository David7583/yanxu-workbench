import { useEffect, useState } from 'react';
import { useEditor, EditorContent } from '@tiptap/react';
import StarterKit from '@tiptap/starter-kit';
import { Markdown } from '@tiptap/markdown';
import { Bold, Italic, Heading2, List, Quote, Undo2, Redo2, LockKeyhole, LockKeyholeOpen, Code2 } from 'lucide-react';

import { normalizeCitationMarkdown, needsSource } from './editor-utils';

export default function Editor({value,onChange,locked,onLock,placeholder='从一个想法开始，写下你的第一段文字…'}:{value:string;onChange:(value:string)=>void;locked:boolean;onLock:()=>void;placeholder?:string}) {
 const [sourceMode,setSourceMode]=useState(()=>needsSource(value));
 const editor=useEditor({extensions:[StarterKit,Markdown],content:value,contentType:'markdown',editable:!locked,onUpdate:({editor})=>onChange(normalizeCitationMarkdown(editor.getMarkdown())),editorProps:{attributes:{class:'paper-prose','aria-label':'论文正文编辑器','data-placeholder':placeholder}}});
 useEffect(()=>{if(needsSource(value))setSourceMode(true);if(editor&&normalizeCitationMarkdown(editor.getMarkdown())!==value)editor.commands.setContent(value,{contentType:'markdown',emitUpdate:false})},[value,editor]);
 useEffect(()=>{editor?.setEditable(!locked)},[locked,editor]);
 if(!editor)return null;
 const tools=[{icon:Bold,name:'加粗',run:()=>editor.chain().focus().toggleBold().run()},{icon:Italic,name:'斜体',run:()=>editor.chain().focus().toggleItalic().run()},{icon:Heading2,name:'二级标题',run:()=>editor.chain().focus().toggleHeading({level:2}).run()},{icon:List,name:'项目列表',run:()=>editor.chain().focus().toggleBulletList().run()},{icon:Quote,name:'引用块',run:()=>editor.chain().focus().toggleBlockquote().run()},{icon:Undo2,name:'撤销',run:()=>editor.chain().focus().undo().run()},{icon:Redo2,name:'重做',run:()=>editor.chain().focus().redo().run()}];
 return <div className={'rich-editor '+(locked?'is-locked':'')}><div className="editor-toolbar">{tools.map(t=><button key={t.name} className="icon-button" title={t.name} aria-label={t.name} disabled={locked||sourceMode} onClick={t.run}><t.icon size={16}/></button>)}<button className={'icon-button '+(sourceMode?'active':'')} title="Markdown 源文" aria-label="Markdown 源文" onClick={()=>{if(!needsSource(value))setSourceMode(!sourceMode)}}><Code2 size={16}/></button><span className="toolbar-space"/><button className={'text-button '+(locked?'active':'')} title="锁定后禁止编辑与接受 AI 改稿" onClick={onLock}>{locked?<LockKeyhole size={14}/>:<LockKeyholeOpen size={14}/>} {locked?'已锁定':'保护正文'}</button></div>{sourceMode?<><div className="source-mode-note">源文模式 · 完整保留公式、表格与引用，可切换阅读预览检查内容。</div><textarea className="markdown-source" aria-label="Markdown 源文编辑器" readOnly={locked} value={value} onChange={e=>onChange(e.target.value)} spellCheck={false}/></>:<div className="paper-scroll"><EditorContent editor={editor}/></div>}</div>
}
