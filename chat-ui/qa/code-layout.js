// Playwright CLI run-code; isolated APIs, no model calls or billing.
async (page) => {
  const c = await page.context().browser().newContext({viewport:{width:1440,height:1050}});
  const p = await c.newPage(); const errors=[];p.on('pageerror',e=>errors.push(e.message));
  const fence=String.fromCharCode(96).repeat(3);
  const code=Array.from({length:72},(_,i)=>`result_${i+1} = attention(query, key, value, dropout_p=0.0)  # Keep the input shape and preserve residual information`).join('\n');
  const markdown='# PyTorch 代码实现\n\n完整展示实现过程，聊天内容区使用适中的宽度。\n\n'+fence+'python\n'+code+'\n'+fence+'\n\n## 代码末尾说明\n\n以上代码应完整展开，纵向滚动由聊天内容区承载。\n\n'+fence+'text\n'+('a'.repeat(300))+'\n'+fence;
  const record={id:'44444444-4444-4444-a444-444444444444',title:'长代码阅读',model:'qwen3.8-flash',version:1,updated:Date.now(),messages:[{id:'u',role:'user',content:'展示完整代码。'},{id:'a',role:'assistant',model:'qwen3.8-flash',content:markdown}]};
  await c.addInitScript(()=>{localStorage.setItem('auth_user',JSON.stringify({id:-850}));localStorage.setItem('auth_token','isolated-layout-fixture');});
  await c.route('**/api/v1/chat/models',r=>r.fulfill({json:{data:[{id:'qwen3.8-flash'}]}}));
  await c.route('**/api/v1/user/chat/**',r=>r.fulfill({json:{data:new URL(r.request().url()).pathname.endsWith('/conversations')?[{...record,messages:undefined}]:record}}));
  const assert=(v,m)=>{if(!v)throw Error(m);};
  try {
    await p.goto('https://aerisun.top/sub2api/chat-ui/index.html');
    await p.getByRole('heading',{name:'PyTorch 代码实现',exact:true}).waitFor();
    await p.locator('[data-streamdown="code-block"] [style*="--sdm-c"]').first().waitFor();
    const after=await p.locator('[data-streamdown="code-block-body"]').first().evaluate(el=>getComputedStyle(el).maxHeight==='none');
    assert(after,'code blocks still have a height cap');
    const phase='width-900'; const measurements=[];
    for(const width of [1440,1920,390]) {
      await p.setViewportSize({width,height:width===390?844:1050});
      await p.locator('.conversation').hover();await p.mouse.wheel(0,-20000);await p.waitForTimeout(250);
      await p.getByRole('heading',{name:'PyTorch 代码实现',exact:true}).scrollIntoViewIfNeeded();
      const m=await p.evaluate(()=>{
        const body=document.querySelector('[data-streamdown="code-block-body"]');const pre=body.querySelector('pre');const b=e=>e.getBoundingClientRect();
        const conv=document.querySelector('.conversation');const scroller=[...conv.querySelectorAll('*')].find(e=>getComputedStyle(e).overflowY==='auto'&&e.scrollHeight>e.clientHeight);
        return{viewport:innerWidth,main:b(document.querySelector('.chat-main')).width,content:b(document.querySelector('.conversation-content')).width,message:b(document.querySelector('.is-assistant .message-content')).width,composer:b(document.querySelector('.composer')).width,codeWidth:body.clientWidth,codeScrollWidth:body.scrollWidth,preWidth:pre.clientWidth,preScrollWidth:pre.scrollWidth,codeHeight:body.clientHeight,codeScrollHeight:body.scrollHeight,maxHeight:getComputedStyle(body).maxHeight,conversationScrollable:!!scroller,pageOverflow:document.documentElement.scrollWidth>innerWidth};
      });measurements.push(m);
      if(after){assert(m.codeHeight>1500&&m.codeScrollHeight<=m.codeHeight+1,'code still has vertical scrolling');assert(m.conversationScrollable&&!m.pageOverflow,'scroll ownership is incorrect');if(width>=1440)assert(Math.abs(m.content-900)<1&&Math.abs(m.composer-844)<1,'desktop content and composer do not respect the requested 900px width');}
      if(width!==1920) await p.screenshot({path:'/data/wenbo/services/sub2api/output/playwright/chat-code-layout-'+phase+'-'+width+'.png',fullPage:true});
      if(width===390){const x=await p.locator('[data-streamdown="code-block-body"] pre').first().evaluate(el=>{el.scrollLeft=100;return el.scrollLeft;});if(after)assert(x>0,'mobile horizontal code scroll unavailable');}
    }
    if(after){await p.setViewportSize({width:1440,height:1050});await p.evaluate(()=>document.documentElement.classList.add('dark'));await p.screenshot({path:'/data/wenbo/services/sub2api/output/playwright/chat-code-layout-after-dark.png',fullPage:true});
      await p.getByRole('heading',{name:'代码末尾说明',exact:true}).scrollIntoViewIfNeeded();assert(await p.getByRole('heading',{name:'代码末尾说明',exact:true}).isVisible(),'bottom of code unreachable');
      const last=await p.locator('[data-streamdown="code-block-body"] pre').last().evaluate(el=>{el.scrollLeft=100;return el.scrollLeft;});assert(last>0,'extra-long lines cannot scroll');}
    assert(errors.length===0,errors.join('; '));return{phase,measurements,errors,entry:await p.locator('script[type=module]').last().getAttribute('src')};
  }finally{await c.close();}
}
