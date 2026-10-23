// Playwright CLI run-code; isolated fixture APIs, no billed requests.
async(page)=>{
 const c=await page.context().browser().newContext({viewport:{width:1440,height:1050}});const p=await c.newPage();const errors=[],writes=[];p.on('pageerror',e=>errors.push(e.message));
 await c.addInitScript(()=>{localStorage.setItem('auth_user',JSON.stringify({id:-851}));localStorage.setItem('auth_token','isolated-welcome-fixture');});
 await c.route('**/api/v1/chat/models',r=>r.fulfill({json:{data:[{id:'qwen3.8-flash'}]}}));await c.route('**/api/v1/user/chat/**',r=>{if(r.request().method()!=='GET')writes.push(r.request().method());return r.fulfill({json:{data:[]}})});
 const assert=(v,m)=>{if(!v)throw Error(m);};const results=[];
 try{
  await p.goto('https://aerisun.top/sub2api/chat-ui/index.html',{waitUntil:'domcontentloaded',timeout:60000});await p.getByRole('heading',{name:'今天想聊些什么？',exact:true}).waitFor({timeout:60000});await p.getByRole('button',{name:'解释一个学习术语',exact:true}).waitFor();
  assert(await p.getByText('写作、编程、学习，让合适的模型帮你一起完成。',{exact:true}).count()===0,'redundant description remains');
  const group=p.getByRole('group',{name:'试试这些问题',exact:true});assert(await group.getByRole('button').count()===2,'expected two starter prompts');
  for(const width of [1440,390,320]){
   await p.setViewportSize({width,height:width===1440?1050:width===390?844:740});await p.waitForTimeout(150);
   const m=await p.evaluate(()=>{const rect=e=>{const b=e.getBoundingClientRect();return{x:b.x,y:b.y,width:b.width,height:b.height,cx:b.x+b.width/2}};const group=document.querySelector('.welcome-suggestions');return{viewport:innerWidth,main:rect(document.querySelector('.chat-main')),icon:rect(document.querySelector('.welcome-icon')),heading:rect(document.querySelector('.welcome-state h2')),group:rect(group),buttons:[...group.querySelectorAll('button')].map(rect),composer:rect(document.querySelector('.composer')),content:rect(document.querySelector('.conversation-content')),horizontalOverflow:document.documentElement.scrollWidth>innerWidth,gridOverflow:group.scrollWidth>group.clientWidth,buttonShadows:[...group.querySelectorAll('button')].map(e=>getComputedStyle(e).boxShadow)}});
   results.push(m);assert(!m.horizontalOverflow&&!m.gridOverflow,'unexpected horizontal scrolling');assert([m.icon,m.heading,m.group,m.composer].every(r=>Math.abs(r.cx-m.main.cx)<2),'welcome content is off center');assert(Math.abs(m.buttons[0].width-m.buttons[1].width)<1&&Math.abs(m.buttons[0].height-m.buttons[1].height)<1,'starter buttons do not align');assert(m.buttonShadows.every(v=>v==='none'),'heavy button shadows remain');
   if(width===1440){assert(m.content.width===900,'900px content width changed');assert(Math.abs(m.buttons[0].y-m.buttons[1].y)<1,'desktop starters are not in one row');}else assert(Math.abs(m.buttons[0].x-m.buttons[1].x)<1&&m.buttons[1].y>m.buttons[0].y,'mobile starters are not stacked');
   await p.screenshot({path:'/data/wenbo/services/sub2api/output/playwright/chat-welcome-'+width+'.png',fullPage:true});
  }
  await p.setViewportSize({width:1440,height:1050});
  for(const [title,prompt]of[
   ['解释一个学习术语','请用通俗的语言解释机器学习中的「过拟合」，举一个生活中的例子，并说明如何避免。'],
   ['生成一张图片','请生成一张图片：一只小猫坐在月球上读书，远处是蓝色地球，柔和的光线，温暖梦幻的插画风格。']]){
   await p.getByRole('button',{name:title,exact:true}).click();const textarea=p.getByRole('textbox',{name:'消息',exact:true});assert(await textarea.inputValue()===prompt,'starter does not fill the expected prompt');assert(await textarea.evaluate(el=>document.activeElement===el),'starter does not focus composer');
  }
  await p.getByRole('textbox',{name:'消息',exact:true}).fill('');await p.evaluate(()=>document.documentElement.classList.add('dark'));await p.mouse.move(1200,80);await p.waitForTimeout(250);await p.screenshot({path:'/data/wenbo/services/sub2api/output/playwright/chat-welcome-dark.png',fullPage:true});assert(errors.length===0,errors.join('; '));assert(writes.length===0,'starter unexpectedly submitted a request');
  return{results,promptsFillAndFocus:true,noAutomaticSubmit:true,errors,entry:await p.locator('script[type=module]').last().getAttribute('src')};
 }finally{await c.close();}
}
