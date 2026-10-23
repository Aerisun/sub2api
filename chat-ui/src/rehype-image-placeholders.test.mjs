import { test } from 'node:test';
import assert from 'node:assert/strict';
import { rehypeImagePlaceholders } from './rehype-image-placeholders.ts';
const image = src => ({type:'element',tagName:'img',properties:{src,alt:'Kitten fishing illustration'},children:[]});
const paragraph = (...children) => ({type:'element',tagName:'p',children});
const text = value => ({type:'text',value});
test('empty repeated image and orphan punctuation disappear while normal body and real image remain', () => {
 const normal=paragraph(text('小猫在池塘边钓鱼。'));
 const valid=paragraph(image('https://aerisun.top/sub2api/image-hub/results/test.png'));
 const tree={type:'root',children:[valid,normal,paragraph(image(''),text(' ， '))]};
 rehypeImagePlaceholders()(tree);
 assert.deepEqual(tree.children,[valid,normal]);
});
test('preserves adjacent text, code examples and unsafe URLs for the existing hardener', () => {
 const unsafe=image('javascript:alert(1)');
 const code={type:'element',tagName:'code',children:[text('![Example]()')]};
 const tree={type:'root',children:[paragraph(text('说明'),image('  ')),paragraph(unsafe),code]};
 rehypeImagePlaceholders()(tree);
 assert.deepEqual(tree.children[0].children,[text('说明')]);
 assert.equal(tree.children[1].children[0],unsafe);
 assert.deepEqual(code.children,[text('![Example]()')]);
});
