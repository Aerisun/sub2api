import { test } from 'node:test';
import assert from 'node:assert/strict';
import { supportsImages, supportsNativeImages, persistImages, persistRecognitionImages, toChatMessages, historyImageChoices, recognitionImageInputs, supplyGenerationImages } from './images.ts';
const image = { url: 'data:image/png;base64,aGVsbG8=', name: 'test.png', mediaType: 'image/png' };
test('vision availability follows the shared model catalog', () => {
  assert.equal(supportsImages('qwen3.8-flash'), true);
  assert.equal(supportsImages('moonshot/kimi-k3'), true);
  assert.equal(supportsImages('nemotron-3.5-lightning-30b-a3b'), false);
  for (const model of ['Nemotron-3.5', 'nemotron-3.5', 'nvidia/Nemotron-3.5']) {
    assert.equal(supportsImages(model), true, model);
  }
  assert.equal(supportsImages('unknown-model'), false);
  for (const retired of ['glm-5.2', 'glm-5.3', 'glm-5.3-flash', 'step-5-preview']) assert.equal(supportsImages(retired), false);
});
test('text-only models use browser-only recognition uploads while native image models retain their route',()=>{
  for(const model of ['Nemotron-3.5','nvidia/Nemotron-3.5','unknown-model'])assert.equal(supportsNativeImages(model),false,model);
  for(const model of ['qwen3.8-flash','gpt-6.1-sol','gpt-6.1-terra'])assert.equal(supportsNativeImages(model),true,model);
  for(const retired of ['glm-5.2','glm-5.3','glm-5.3-flash','step-5-preview'])assert.equal(supportsNativeImages(retired),false,retired);
});
test('background sync preserves browser-only images without opening files or uploading them',async()=>{
  const local={clientID:'85e9d563-7808-4ee7-8ef1-6372b130b0d4',name:'local.png',mediaType:'image/png'};
  const items=[{id:'user-message',role:'user',content:'图片',images:[local]}];
  // No browser storage/network globals exist in this Node process. These calls
  // must preserve metadata without trying to recover or upload original bytes.
  assert.deepEqual(await persistImages(items),items);
  assert.deepEqual(await persistRecognitionImages(items,true),items);
});
test('image-only messages remain in retries and later conversation context', () => {
  const history = [{ role: 'user', content: '', images: [image] },
    { role: 'assistant', content: '图像描述', reasoning: '分析图像' },
    { role: 'assistant', content: '', error: 'failed' },
    { role: 'user', content: '然后呢' }];
  assert.deepEqual(toChatMessages(history), [
    { role: 'user', content: '\n[历史图片已在前文处理，本轮不重新识别。]' },
    { role: 'assistant', content: '图像描述', reasoning_content: '分析图像' },
    { role: 'user', content: '然后呢' },
  ]);
  assert.equal(toChatMessages(history.slice(0,1))[0].content[0].image_url.url, image.url);
});
test('mixed text and multiple images preserve order and ordinary text stays compatible', () => {
  const messages = toChatMessages([{role:'user',content:'比较图片',images:[image, {...image,name:'two.png'}]}]);
  assert.deepEqual(messages[0].content.map(p=>p.type), ['text','image_url','image_url']);
  assert.equal(toChatMessages([{role:'user',content:'你好'}])[0].content, '你好');
});

test('ordinary text follow-ups never reopen or resend historical originals',async()=>{
  const first={clientID:'first',name:'first.png',mediaType:'image/png'};
  const second={clientID:'second',name:'second.png',mediaType:'image/png'};
  const history=[{id:'u1',role:'user',content:'',images:[first,second]},
    {id:'a1',role:'assistant',content:'结果',tools:[{name:'image_recognition',status:'completed',output:'第一张',input:{images:[first]}}]},
    {id:'u2',role:'user',content:'继续',recognitionImages:['browser:first']}];
  assert.deepEqual(historyImageChoices(history).map(choice=>[choice.key,choice.recognized]),[['browser:first',true],['browser:second',false]]);
  // Browser storage is intentionally absent. Legacy manual-selection metadata
  // cannot cause a new read or charge on a normal follow-up.
  assert.deepEqual(await recognitionImageInputs(history),{});
});

test('browser image delivery rejects oversized or duplicated model requests before reading any original',async()=>{
  const controller=new AbortController();
  await assert.rejects(supplyGenerationImages('job',{id:'request',imageIDs:Array(13).fill('id')},[],controller.signal),/图片读取请求无效/);
  await assert.rejects(supplyGenerationImages('job',{id:'request',imageIDs:['id','id']},[],controller.signal),/图片读取请求无效/);
});

test('model-requested browser delivery sends only referenced images and reports absent originals',async()=>{
  const previous={fetch:globalThis.fetch,location:globalThis.location,localStorage:globalThis.localStorage};
  const requests=[];
  globalThis.location={pathname:'/sub2api/chat-ui/index.html'};
  globalThis.localStorage={getItem:key=>key==='auth_user' ? '{"id":42}' : 'fixture-token'};
  globalThis.fetch=async(url,init)=>{requests.push({url,body:JSON.parse(init.body)});return new Response('{"code":0,"data":{"accepted":true}}',{status:200,headers:{'Content-Type':'application/json'}})};
  try {
    const images=[{clientID:'first',name:'a.png',mediaType:'image/png',url:'first-original'},
      {clientID:'second',name:'b.png',mediaType:'image/png',url:'second-original'}];
    await supplyGenerationImages('generation',{id:'model-request',imageIDs:['second','unavailable']},[{id:'user',role:'user',images}],new AbortController().signal);
    assert.equal(requests.length,1);
    assert.equal(requests[0].url,'/sub2api/api/v1/user/chat/generations/generation/image-inputs');
    assert.deepEqual(requests[0].body,{requestID:'model-request',images:{second:'second-original'},unavailable:['unavailable']});
    const controller=new AbortController();controller.abort();
    await assert.rejects(supplyGenerationImages('generation',{id:'cancelled',imageIDs:['first']},[{role:'user',images}],controller.signal),{name:'AbortError'});
    assert.equal(requests.length,1);
  } finally {for(const [key,value] of Object.entries(previous)){if(value===undefined)delete globalThis[key];else globalThis[key]=value;}}
});
