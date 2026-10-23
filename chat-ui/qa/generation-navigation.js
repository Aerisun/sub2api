// Playwright CLI run-code fixture: all API requests are intercepted; no billed model calls.
async (page) => {
  const target = 'https://aerisun.top/sub2api/chat-ui/index.html';
  const context = await page.context().browser().newContext({ viewport: { width: 1440, height: 900 } });
  const p = await context.newPage();
  const png = Buffer.from('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', 'base64');
  let failureReleased = false;
  const errors = [], starts = [], saves = [], streams = [], held = new Map();
  const a = '11111111-1111-4111-a111-111111111111', b = '22222222-2222-4222-a222-222222222222';
  const records = new Map([
    [a, { id:a,title:'A 项目规划',model:'qwen3.8-flash',version:1,updated:Date.now(),messages:[{id:'a-user',role:'user',content:'整理项目规划'},{id:'a-job',role:'assistant',content:'',model:'qwen3.8-flash'}],generation:{id:'a-job',status:'running',sequence:0} }],
    [b, { id:b,title:'B 历史会话',model:'qwen3.8-flash',version:1,updated:Date.now()-1000,messages:[{id:'b-user',role:'user',content:'历史问题'},{id:'b-reply',role:'assistant',content:'B 历史回答',model:'qwen3.8-flash'}] }]
  ]);
  const assert = (value, message) => { if (!value) throw new Error(message); };
  const waitFor = async (condition, message) => { for(let i=0;i<100;i++){if(condition())return;await p.waitForTimeout(50);}throw new Error(message); };
  p.on('pageerror', error => errors.push(error.message));
  await context.addInitScript(() => { localStorage.setItem('auth_user',JSON.stringify({id:-820})); localStorage.setItem('auth_token','isolated-ui-fixture'); document.addEventListener('DOMContentLoaded',()=>document.documentElement.classList.add('dark'),{once:true}); });
  await context.route('**/api/v1/chat/models',r=>r.fulfill({json:{data:['qwen3.8-flash','qwen3.8-max'].map(id=>({id}))}}));
  await context.route('**/api/v1/user/chat/**', async route => {
    const request = route.request(), url = new URL(request.url()), path=url.pathname, method=request.method();
    if(path.endsWith('/images') && method==='POST')return route.fulfill({json:{data:{assetID:'33333333-3333-4333-a333-333333333333'}}});
    if(path.includes('/images/'))return route.fulfill({body:png,contentType:'image/png'});
    if(path.endsWith('/conversations'))return route.fulfill({json:{data:[...records.values()].map(({messages,generation,...meta})=>meta)}});
    if(path.endsWith('/stop')){
      const record=[...records.values()].find(c=>path.includes(c.generation?.id));
      if(record){record.generation.status='stopped';record.messages.at(-1).error='已停止生成';record.version++;}
      return route.fulfill({json:{data:{stopping:true}}});
    }
    if(path.endsWith('/generations') && method==='POST'){
      const body=request.postDataJSON();starts.push(body);
      if(starts.length>2){await new Promise(resolve=>held.set(body.chat.id,resolve));failureReleased=true;return route.fulfill({status:429,json:{message:'会话专属的测试错误'}});}
      const previous=records.get(body.chat.id);
      const record={...body.chat,messages:body.append?[...previous.messages,...body.chat.messages]:body.chat.messages,version:(previous?.version||0)+1,updated:Date.now(),generation:{id:body.requestID,status:'running',sequence:0}};
      records.set(record.id,record);
      if(starts.length<=2){await new Promise(resolve=>held.set(record.id,resolve));}
      else return route.fulfill({status:429,json:{message:'会话专属的测试错误'}});
      return route.fulfill({status:202,json:{data:{...record,messages:undefined}}});
    }
    if(path.includes('/generations/') && path.endsWith('/events')){
      streams.push(path);
      await new Promise(resolve=>setTimeout(resolve,200));
      const record=[...records.values()].find(c=>path.includes(c.generation?.id));
      if(!record)return route.fulfill({status:404,json:{message:'missing fixture job'}});
      const content=Array.from(record.messages.at(-1).content||'').slice(Number(url.searchParams.get('content_offset'))).join('');
      return route.fulfill({contentType:'text/event-stream',body:'data: '+JSON.stringify({generation:record.generation,content,reasoning:''})+'\n\n'});
    }
    const chatID=path.split('/conversations/')[1];
    if(method==='GET')return route.fulfill({json:{data:records.get(chatID)}});
    if(method==='PUT'){
      saves.push(chatID);const value=request.postDataJSON();value.version=(records.get(chatID)?.version||0)+1;records.set(chatID,value);return route.fulfill({json:{data:value}});
    }
    if(method==='DELETE'){records.delete(chatID);return route.fulfill({json:{data:{deleted:true}}});}
    return route.fulfill({status:400,json:{message:'unexpected fixture request'}});
  });
  try {
    await p.goto(target);
    const text=p.getByRole('textbox',{name:'消息'}), plus=p.getByRole('button',{name:'新建会话',exact:true});
    const title = value => p.locator('.history-title').filter({hasText:value});
    await title('A 项目规划').waitFor();
    await p.getByRole('button',{name:'停止生成',exact:true}).waitFor();
    const controls = { input:await text.isEnabled(),newChat:await plus.isEnabled(),history:await title('B 历史会话').isEnabled(),effort:await p.getByRole('combobox',{name:'思考强度',exact:true}).isEnabled(),model:await p.getByRole('combobox',{name:'选择模型',exact:true}).isEnabled(),upload:await p.getByRole('button',{name:'上传图片',exact:true}).isEnabled(),otherDelete:await p.getByRole('button',{name:'删除对话：B 历史会话',exact:true}).isEnabled() };
    assert(Object.values(controls).every(Boolean),'a generation still locks controls: '+JSON.stringify(controls));
    await text.fill('A 的下一条草稿');await p.locator('input[type=file]').setInputFiles({name:'chat-image-test.png',mimeType:'image/png',buffer:png});
    await p.getByRole('combobox',{name:'选择模型',exact:true}).click();await p.getByRole('option').filter({has:p.getByText('qwen3.8-max',{exact:true})}).click();
    await text.press('Enter');await p.waitForTimeout(100);assert(starts.length===0,'Enter submitted while current reply is running');
    await title('B 历史会话').click();await p.getByText('B 历史回答',{exact:true}).waitFor();
    assert(await text.inputValue()==='','A text leaked into B');assert(await p.locator('.image-attachment').count()===0,'A image leaked into B');
    await text.fill('B 的草稿');await p.locator('input[type=file]').setInputFiles({name:'chat-next-test.png',mimeType:'image/png',buffer:png});
    await title('A 项目规划').click();
    assert(await text.inputValue()==='A 的下一条草稿','A draft not restored');assert(await p.locator('.image-attachment').count()===1,'A image draft not restored');
    assert((await p.getByRole('combobox',{name:'选择模型',exact:true}).innerText()).includes('qwen3.8-max'),'A model choice not restored');
    await plus.click();await text.fill('C 独立请求');await p.locator('input[type=file]').setInputFiles({name:'chat-image-test.png',mimeType:'image/png',buffer:png});await p.getByRole('button',{name:'发送消息',exact:true}).click();
    await waitFor(()=>starts.length===1,'C did not start');const cID=starts[0].chat.id;
    await text.fill('C 生成时继续编辑');await p.locator('input[type=file]').setInputFiles({name:'chat-next-test.png',mimeType:'image/png',buffer:png});
    await title('B 历史会话').click();assert(await text.inputValue()==='B 的草稿','B draft not restored during C preparation');assert(await p.locator('.image-attachment').count()===1,'B image draft not restored');
    await text.fill('B 独立请求');await p.getByRole('button',{name:'发送消息',exact:true}).click();
    await waitFor(()=>starts.length===2,'B could not start while C was preparing');
    await text.fill('B 新的草稿');
    await title('C 独立请求').click();assert(await text.inputValue()==='C 生成时继续编辑','C text changed by B submission');
    assert(await p.locator('.image-attachment').count()===2,'C attachment editing did not work while preparing');
    held.get(cID)();await p.getByRole('button',{name:'停止生成',exact:true}).waitFor();
    await p.getByRole('button',{name:'移除图片：chat-next-test.png',exact:true}).waitFor();
    await p.waitForTimeout(120);
    assert(await p.locator('.image-attachment').count()===1,'start completion removed new draft image or retained sent image');
    assert(await text.inputValue()==='C 生成时继续编辑','start completion cleared next message');
    held.get(b)();await p.waitForTimeout(300);assert(await text.inputValue()==='C 生成时继续编辑','B acceptance changed active C draft');
    records.get(a).generation.status='completed';records.get(a).version++;records.get(a).messages.at(-1).content='A 已在后台完成';
    await waitFor(()=>streams.some(path=>path.includes('a-job')),'A background subscription missing');
    await title('A 项目规划').locator('[aria-label="正在生成"]').waitFor({state:'detached',timeout:10000});
    await title('A 项目规划').click();await p.getByText('A 已在后台完成',{exact:true}).waitFor();
    assert(await text.inputValue()==='A 的下一条草稿','background completion erased A draft');assert((await p.getByRole('combobox',{name:'选择模型',exact:true}).innerText()).includes('qwen3.8-max'),'background completion reset chosen model');
    await title('C 独立请求').click();await p.screenshot({path:'/data/wenbo/services/sub2api/output/playwright/chat-unlocked-desktop.png',fullPage:true});
    await p.setViewportSize({width:390,height:844});await p.getByRole('button',{name:'历史对话',exact:true}).click();assert(await plus.isEnabled(),'mobile new chat locked');await title('B 历史会话').click();assert(await text.inputValue()==='B 新的草稿','mobile navigation lost B draft');
    await p.getByRole('button',{name:'历史对话',exact:true}).click();await title('C 独立请求').click();await p.screenshot({path:'/data/wenbo/services/sub2api/output/playwright/chat-unlocked-mobile.png',fullPage:true});
    const overflow=await p.evaluate(()=>document.documentElement.scrollWidth>innerWidth);
    await p.getByRole('button',{name:'停止生成',exact:true}).click();await p.getByRole('button',{name:'发送消息',exact:true}).waitFor({timeout:10000});
    assert(records.get(b).generation.status==='running','stopping C affected B');
    assert(await text.inputValue()==='C 生成时继续编辑','stopping cleared C draft');
    assert(starts.length===2,'navigation duplicated a model request');const preparationSaves=[...saves];assert(preparationSaves.length===0,'preparing conversations were auto-saved concurrently');
    assert(!overflow,'mobile horizontal overflow');assert(errors.length===0,'browser runtime error: '+errors.join(','));
    await p.getByRole('button',{name:'发送消息',exact:true}).click();
    await waitFor(()=>starts.length===3,'C follow-up did not start after stopping');
    await p.getByRole('button',{name:'历史对话',exact:true}).click();await title('B 历史会话').click();
    held.get(cID)();await waitFor(()=>failureReleased,'fixture rejection was not released');await p.waitForTimeout(200);
    assert(await p.getByRole('alert').count()===0,'C failure leaked into active B');assert(await text.inputValue()==='B 新的草稿','C failure changed B draft');
    await p.getByRole('button',{name:'历史对话',exact:true}).click();await title('C 独立请求').click();await p.getByRole('alert').filter({hasText:'会话专属的测试错误'}).waitFor();
    const loadedEntry=await p.locator('script[type=module]').getAttribute('src');
    return {phase:'after',loadedEntry,errorIsolated:true,target,controls,requests:starts.map(s=>({chatID:s.chat.id,model:s.chat.model,text:s.chat.messages.at(-2).content})),separateDrafts:true,attachmentDrafts:true,newImagesRetained:true,backgroundCompleted:true,backgroundStreams:streams.length,stopIsolated:true,preparingSaveRaces:preparationSaves,allSaves:saves,overflow,errors};
  } finally {for(const resolve of held.values())resolve();await context.close();}
}
