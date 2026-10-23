// Public UI with isolated fixture APIs and images; no billed model requests.
async (page) => {
 const root='/data/wenbo/services/sub2api';
 const stage='after';
 const c=await page.context().browser().newContext({viewport:{width:1280,height:900}});
 const p=await c.newPage(),errors=[];p.on('pageerror',e=>errors.push(e.message));
 const models=['gpt-image-2.5-flare','gpt-image-2.5-sunburst','gpt-image-2'];
 const id='77777777-7777-4777-a777-777777777777';
 const makeMessage=(model,index)=>({id:'a'+index,role:'assistant',model:'gpt-6-sol',reasoning:'图片已经生成。',content:`![生成的图片](https://aerisun.top/sub2api/image-hub/results/model-fixture-${index}.png)`,tools:[{type:'tool_call',id:'image-'+index,name:'image_generation',status:'completed',...(model?{metadata:{model}}:{}),output:`![生成的图片](https://aerisun.top/sub2api/image-hub/results/model-fixture-${index}.png)`}]});
 const record={id,title:'图片模型显示检查',model:'gpt-6-sol',version:1,updated:Date.now(),messages:[{id:'u',role:'user',content:'生成图片并显示所用模型'},...models.map(makeMessage),makeMessage('',3),{id:'failed',role:'assistant',model:'gpt-6-sol',content:'图片生成失败',tools:[{type:'tool_call',id:'failed-tool',name:'image_generation',status:'failed',error:'模拟失败',metadata:{model:'failed-fixture-model'}}]}]};
 let streamed=false;
 const assert=(value,message)=>{if(!value)throw Error(message);};
 await c.addInitScript(()=>{localStorage.setItem('auth_user',JSON.stringify({id:-921}));localStorage.setItem('auth_token','isolated-image-model-fixture');document.addEventListener('DOMContentLoaded',()=>document.documentElement.classList.add('dark'));});
 await c.route('**/api/v1/chat/models',r=>r.fulfill({json:{data:[{id:'gpt-6-sol'}]}}));
 await c.route('**/image-hub/results/model-fixture-*.png',r=>r.fulfill({contentType:'image/svg+xml',body:'<svg xmlns="http://www.w3.org/2000/svg" width="480" height="180"><rect width="480" height="180" fill="#dcebe7"/><circle cx="240" cy="90" r="48" fill="#628f80"/></svg>'}));
 await c.route('**/api/v1/user/chat/**',async r=>{
  const path=new URL(r.request().url()).pathname;
  if(path.endsWith('/events')){
   const tool={...record.messages[1].tools[0],status:streamed?'completed':'running'};
   return r.fulfill({contentType:'text/event-stream',body:'data: '+JSON.stringify({generation:{...record.generation,status:streamed?'completed':'running'},tools:[tool]})+'\n\n'});
  }
  return r.fulfill({json:{data:path.endsWith('/conversations')?[{...record,messages:undefined}]:record}});
 });
 try{
  await p.goto('https://aerisun.top/sub2api/chat-ui/index.html');await p.getByText('生成图片并显示所用模型',{exact:true}).waitFor();
  const labels=p.locator('.image-generation-models');
  await p.locator('.generated-image-preview[data-image-ready=true]').first().waitFor();
  await p.locator('.conversation').hover();await p.mouse.wheel(0,-10000);await p.waitForTimeout(200);
  const first=p.locator('.is-assistant').first();await first.scrollIntoViewIfNeeded();
  await first.screenshot({path:root+'/output/playwright/image-tool-model-'+stage+'-desktop.png'});
  if(stage==='before')return{stage,visibleModelLabels:await labels.count(),entry:await p.locator('script[type=module]').getAttribute('src'),errors};
  assert(await labels.count()===4,'completed image labels missing or failed tool mislabeled');
  for(const model of models)assert(await labels.filter({hasText:model}).count()>=1,'missing model '+model);
  assert(await labels.filter({hasText:'gpt-6-sol'}).count()===0,'chat model substituted for image model');
  assert(await labels.filter({hasText:'未记录'}).count()===1,'legacy result guesses a model');
  const collapsed=first.locator('[data-message-thinking]');assert(await collapsed.getAttribute('data-state')==='closed','thinking is not collapsed');
  assert(await labels.first().isVisible(),'image model hidden with thinking collapsed');
  await collapsed.getByRole('button').first().click();
  const card=first.locator('[data-tool-name=image_generation]');await card.locator('summary').click();assert(await card.getAttribute('open')!==null,'tool result did not open');
  assert(await card.locator('.tool-model').innerText()===models[0],'tool summary model missing');
  assert(await card.getByRole('link',{name:'查看图片',exact:true}).getAttribute('href')===record.messages[1].content.match(/\(([^)]+)\)/)[1],'image link changed');
  await collapsed.getByRole('button').first().click();
  const viewports=[];
  for(const theme of ['dark','light']){
   await p.evaluate(theme=>document.documentElement.classList.toggle('dark',theme==='dark'),theme);
   await p.setViewportSize({width:390,height:844});await first.scrollIntoViewIfNeeded();
   const overflow=await p.evaluate(()=>document.documentElement.scrollWidth>innerWidth);assert(!overflow,'mobile overflow in '+theme);
   await first.screenshot({path:root+'/output/playwright/image-tool-model-after-mobile-'+theme+'.png'});viewports.push({theme,width:390,overflow});
  }
  record.messages=[record.messages[0],{...makeMessage(models[1],0),id:'stream-job',tools:[{...makeMessage(models[1],0).tools[0],status:'running'}]}];record.generation={id:'stream-job',status:'running',sequence:1};record.version++;
  await p.reload();await p.locator('[data-tool-status=running]').waitFor({state:'attached'});assert(await labels.count()===0,'running image is labeled completed');
  record.messages[1].tools[0].status='completed';record.generation.status='completed';record.version++;streamed=true;
  await labels.first().waitFor({timeout:15000});assert((await labels.first().innerText()).includes(models[1]),'stream completion lost model');
  await p.reload();await labels.first().waitFor();assert((await labels.first().innerText()).includes(models[1]),'history reload lost model');
  assert(errors.length===0,errors.join('; '));
  return{stage,models,legacyModel:'未记录',failedExcluded:true,visibleWithThinkingCollapsed:true,toolExpandable:true,imageLinkPreserved:true,streamCompletion:true,reloadRestoration:true,viewports,errors,entry:await p.locator('script[type=module]').getAttribute('src')};
 }finally{await c.close();}
}
