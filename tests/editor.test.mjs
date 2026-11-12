import test from 'node:test';
import assert from 'node:assert/strict';
import { normalizeCitationMarkdown, needsSource } from '../frontend/src/editor-utils.ts';

test('single and grouped citations survive markdown escaping',()=>{
 assert.equal(normalizeCitationMarkdown('A \\[@ref\\_a\\] B'), 'A [@ref_a] B');
 assert.equal(normalizeCitationMarkdown('\\[@ref\\_a; @ref\\_b\\]'), '[@ref_a; @ref_b]');
 assert.equal(normalizeCitationMarkdown('[@ref_a]'), '[@ref_a]');
 assert.equal(normalizeCitationMarkdown('\\[ordinary\\_text\\]'), '\\[ordinary\\_text\\]');
});
test('unsupported structured content stays in lossless source mode',()=>{
 for(const text of ['| A | B |\n| --- | --- |\n| x | y |','A | B\n--- | ---\nx | y','Formula $E=mc^2$','![figure](image.png)','Claim[^1]','<table><tr><td>1</td></tr></table>'])assert.equal(needsSource(text),true,text);
 assert.equal(needsSource('# Abstract\n\nA paragraph [@ref_abcd].'),false);
});
