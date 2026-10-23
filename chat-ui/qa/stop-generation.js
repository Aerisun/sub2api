async (page) => {
 const c=await page.context().browser().newContext({viewport:{width:1280,height:850}});const p=await c.newPage();const errors=[];p.on('pageerror',e=>errors.push(e.message));
 await c.addInitScript(()=>{localStorage.setItem('auth_user',JSON.stringify({id:-832}));localStorage.setItem('auth_token','isolated-stop-fixture');document.addEventListener('DOMContentLoaded',()=>document.documentElement.classList.add('dark'));});
 const a='11111111-1111-4111-a111-111111111111',b='22222222-2222-4222-a222-222222222222';
 const records=new Map([[a,{id:a,title:'停止测试 A',model:'qwen3.8-flash',version:1,updated:Date.now(),messages:[{id:'user-old',role:'user',content:'旧问题'},{id:'old-job',role:'assistant',content:'旧回答',model:'qwen3.8-flash'}],generation:{id:'old-job',status:'completed',sequence:1}}],[b,{id:b,title:'其他会话 B',model:'qwen3.8-flash',version:1,updated:Date.now()-100,messages:[{id:'user-b',role:'user',content:'B 问题'},{id:'reply-b',role:'assistant',content:'B 回答',model:'qwen3.8-flash'}]}]]);
 let release,submitted;let starts=0;const stops=[];const assert=(x,m)=>{if(!x)throw Error(m);};
 await c.route('**/api/v1/chat/models',r=>r.fulfill({json:{data:[{id:'qwen3.8-flash'}]}}));
 await c.route('**/api/v1/user/chat/**',async r=>{const req=r.request(),url=new URL(req.url()),path=url.pathname,method=req.method();
  if(path.endsWith('/conversations'))return r.fulfill({json:{data:[...records.values()].map(({messages,generation,...meta})=>meta)}});
  if(path.endsWith('/generations')&&method==='POST'){
   starts++;submitted=req.postDataJSON();const old=records.get(submitted.chat.id);const record={...submitted.chat,version:(old?.version||0)+1,messages:submitted.append?[...old.messages,...submitted.chat.messages]:submitted.chat.messages,generation:{id:submitted.requestID,status:'running',sequence:0}};records.set(record.id,record);
   if(starts===1)await new Promise(resolve=>{release=resolve;});
   return r.fulfill({status:202,json:{data:{...record,messages:undefined}}});
  }
  if(path.endsWith('/stop')){
   const id=path.split('/generations/')[1].split('/')[0];stops.push(id);const record=[...records.values()].find(x=>x.generation?.id===id);assert(record,'stop targeted old/unknown request');assert(id!=='old-job','stop targeted previous completed reply');record.generation.status='stopped';record.messages.at(-1).content='保留的部分回答';record.messages.at(-1).reasoning='保留的思考';record.messages.at(-1).error='已停止生成';record.version++;return r.fulfill({json:{data:{stopped:true,stopping:false}}});
  }
  // No completed SSE frame is ever sent. The stop must finish via confirmation.
  if(path.endsWith('/events'))return r.fulfill({contentType:'text/event-stream',body:': heartbeat\n\n'});
  const id=path.split('/conversations/')[1];
  if(method==='GET')return r.fulfill({json:{data:records.get(id)}});
  if(method==='PUT'){const data=req.postDataJSON();data.version++;records.set(id,data);return r.fulfill({json:{data}});}
  return r.fulfill({json:{data:{}}});
 });
 try {
  await p.goto('https://aerisun.top/sub2api/chat-ui/index.html');const text=p.getByRole('textbox',{name:'消息'});await p.getByText('旧回答',{exact:true}).waitFor();await text.fill('取消准备中的回复');await p.getByRole('button',{name:'发送消息',exact:true}).click();
  for(let n=0;n<100&&!release;n++)await p.waitForTimeout(20);assert(release,'start request not observed');const job=submitted.requestID;
  await p.getByRole('button',{name:'停止生成',exact:true}).click();await p.getByRole('button',{name:'正在停止',exact:true}).waitFor();assert(stops.length===0,'stop raced a pending start');
  await text.fill('A 未发送的草稿');await p.locator('.history-title').filter({hasText:'其他会话 B'}).click();await text.fill('B 未发送的草稿');release();
  for(let n=0;n<100&&stops.length<1;n++)await p.waitForTimeout(20);assert(stops[0]===job,'queued stop missed current generation');
  await p.locator('.history-title').filter({hasText:'停止测试 A'}).locator('[aria-label="正在生成"]').waitFor({state:'detached'});assert(await text.inputValue()==='B 未发送的草稿','queued stop changed another draft');
  await p.locator('.history-title').filter({hasText:'停止测试 A'}).click();await p.getByRole('button',{name:'发送消息',exact:true}).waitFor();assert(await text.inputValue()==='A 未发送的草稿','stop cleared input draft');await p.getByText('保留的部分回答',{exact:true}).waitFor();
  await p.getByRole('button',{name:'发送消息',exact:true}).click();await p.getByRole('button',{name:'停止生成',exact:true}).waitFor();await text.fill('停止后继续编辑');const start=Date.now();await p.getByRole('button',{name:'停止生成',exact:true}).click();await p.getByRole('button',{name:'发送消息',exact:true}).waitFor();
  assert(stops.length===2,'normal stop did not reach backend');assert(await text.inputValue()==='停止后继续编辑','normal stop cleared draft');assert(errors.length===0,errors.join(','));
  await p.screenshot({path:'/data/wenbo/services/sub2api/output/playwright/chat-stop-confirmed.png',fullPage:true});
  return {queuedStop:true,correctGeneration:true,confirmationWithoutSSE:true,stopLatencyMs:Date.now()-start,draftsPreserved:true,stops:stops.length,errors,entry:await p.locator('script[type=module]').getAttribute('src')};
 }finally{release?.();await c.close();}
}
