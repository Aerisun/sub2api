import {test} from 'node:test';
import assert from 'node:assert/strict';
import {formatGeminiReasoning} from './reasoning-format.ts';

test('Gemini thought section titles become separate paragraphs during streaming',()=>{
 const text='**Rephrased overfitting concept**\n\nCompared the concept. **Compared overfitting analogies**\n\nCompared a tailor.';
 assert.equal(formatGeminiReasoning(text),'**Rephrased overfitting concept**\n\nCompared the concept.\n\n**Compared overfitting analogies**\n\nCompared a tailor.');
 const prefix=text.slice(0,text.indexOf('analogies')+4);
 assert.equal(formatGeminiReasoning(prefix),prefix);
 assert.equal(formatGeminiReasoning(formatGeminiReasoning(text)),formatGeminiReasoning(text));
});
test('inline emphasis and code retain their original formatting',()=>{
 for(const text of ['Use **training data** carefully.','A sentence with **important words**\n\nNext paragraph.','```md\nText. **Title**\n\nCode\n```','~~~md\nText. **Title**\n\nCode\n~~~'])assert.equal(formatGeminiReasoning(text),text);
});
test('Chinese section titles and CRLF spacing are supported',()=>{
 assert.equal(formatGeminiReasoning('完成分析。**选择类比**\r\n\r\n用裁缝说明。'),'完成分析。\n\n**选择类比**\r\n\r\n用裁缝说明。');
});
test('a single newline before a Gemini title must not collapse into a soft break',()=>{
 const text='Relevant components.\n**Formulated response structure**\n\nOrganized the answer.';
 assert.equal(formatGeminiReasoning(text),'Relevant components.\n\n**Formulated response structure**\n\nOrganized the answer.');
});
