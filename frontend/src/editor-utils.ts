/** Preserve only our structured citation tokens; ordinary Markdown escaping stays intact. */
export function normalizeCitationMarkdown(text:string){
 return text.replace(/\\?\[([^\]\n]+)\\?\]/g,(full,inner:string)=>{
  const clean=inner.replace(/\\_/g,'_').replace(/\\$/,'');
  return /^@ref_[A-Za-z0-9_-]+(?:\s*;\s*@ref_[A-Za-z0-9_-]+)*\s*$/.test(clean)?`[${clean}]`:full;
 });
}
export const needsSource=(text:string)=>/^\s*\|.*\|/m.test(text)||/^\s*\|?\s*:?-{3,}:?\s*\|/m.test(text)||/!\[[^\]]*\]\(|\[\^[^\]]+\]|\$|\\\(|\\\[|<\/?[A-Za-z][^>]*>/.test(text);
