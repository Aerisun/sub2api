// Playwright CLI run-code; fixture APIs only, no billed requests.
async (page) => {
 const mode='production'; const browser=page.context().browser(); const c=await browser.newContext({viewport:{width:1440,height:1050}}); const p=await c.newPage();const errors=[];p.on('pageerror',e=>errors.push(e.message));
 const markdown="# 理解注意力中的信息交换\n\n注意力机制让每个位置从相关信息中获取上下文。**先理解信息来自哪里，再看公式与实现**，就会清楚很多。\n\n## 两种注意力方式\n\n| 模块 | 信息来源 | 适用场景 |\n| :--- | :--- | :--- |\n| **Cross Attention** | 另一条特征分支 | 多视角融合、跨模态交互 |\n| **Self Attention** | 当前特征序列 | 文本理解、序列内部的信息聚合 |\n\n> 直观理解：前者关注另一组信息，后者整理自己已经拥有的信息。\n\n## Self Attention Block（自注意力）\n\n结构与跨注意力相似，但两条分支**完全独立、互不交叉**：\n\n1. 从同一组输入生成查询、键和值。\n2. 计算关联程度，得到每个位置的注意力权重。\n3. 对值进行加权求和，再通过残差连接保留原始信息。\n\n标准计算方式为：\n\n$$\n\\operatorname{Attention}(Q,K,V)=\\operatorname{softmax}\\left(\\frac{QK^{\\mathsf T}}{\\sqrt{d_k}}\\right)V\n$$\n\n### 一个简洁的实现\n\n调用 `scaled_dot_product_attention` 可以避免手动实现整个计算过程。\n\n```python\nimport torch\nimport torch.nn.functional as F\n\n\ndef self_attention(query, key, value):\n    # 使用 PyTorch 内置的注意力算子\n    return F.scaled_dot_product_attention(\n        query, key, value, dropout_p=0.0\n    )\n```\n\n### 工程上的选择\n\n- 保持张量维度一致，留意不同库的维度约定。\n- 在训练阶段配置 dropout，在推理阶段使用 `dropout_p=0.0`。\n  - 输入和输出共享相同的特征空间。\n  - 长序列场景可以使用分块和缓存优化。\n\n- [x] 检查输入形状\n- [ ] 对照实际数据验证效果\n\n---\n\n### 查看数据流\n\n```mermaid\nflowchart LR\n  A[输入序列] --> B[查询与键]\n  B --> C[注意力权重]\n  C --> D[加权输出]\n```\n\n更多交互与渲染细节见 [Streamdown 文档](https://streamdown.ai/docs)。\n";
 let streamText='';
 const id='33333333-3333-4333-a333-333333333333';const record={id,title:'注意力机制与实现',model:'qwen3.8-flash',version:1,updated:Date.now(),messages:[{id:'u',role:'user',content:'解释自注意力，给出公式和实现。'},{id:'a',role:'assistant',model:'qwen3.8-flash',content:markdown}]};
 await c.addInitScript(()=>{localStorage.setItem('auth_user',JSON.stringify({id:-850}));localStorage.setItem('auth_token','isolated-markdown-fixture');});
 await c.route('**/api/v1/chat/models',r=>r.fulfill({json:{data:[{id:'qwen3.8-flash'}]}}));
 await c.grantPermissions(['clipboard-read','clipboard-write']);
 await c.route('**/api/v1/user/chat/**',async r=>{
  const url=new URL(r.request().url());
  if(url.pathname.endsWith('/events')){
   const offset=Number(url.searchParams.get('content_offset'));
   return r.fulfill({contentType:'text/event-stream',body:'data: '+JSON.stringify({generation:record.generation,content:Array.from(streamText).slice(offset).join(''),reasoning:''})+'\n\n'});
  }
  return r.fulfill({json:{data:url.pathname.endsWith('/conversations')?[{...record,messages:undefined}]:record}});
 });
 const assert=(condition,message)=>{if(!condition)throw new Error(message);};
 const screenshot=async(name,heading)=>{
  await p.locator('.conversation').hover();await p.mouse.wheel(0,-10000);await p.waitForTimeout(200);
  await p.getByRole('heading',{name:heading,exact:true}).scrollIntoViewIfNeeded();await p.waitForTimeout(100);
  await p.screenshot({path:'/data/wenbo/services/sub2api/output/playwright/chat-markdown-'+mode+'-'+name+'.png',fullPage:true});
 };
 try {
  await p.goto('https://aerisun.top/sub2api/chat-ui/index.html');
  const title=p.getByRole('heading',{name:'理解注意力中的信息交换',exact:true});await title.waitFor();
  await p.locator('.markdown-body .katex').waitFor();await p.locator('[data-streamdown="mermaid-block"] svg g.node').first().waitFor({timeout:45000});
  const code=p.locator('[data-streamdown="code-block"]').first();await code.locator('[style*="--sdm-c"]').first().waitFor();
  const metrics=await p.locator('.is-assistant .markdown-body').evaluate(el=>{
   const style=e=>getComputedStyle(e),table=el.querySelector('[data-streamdown="table-wrapper"]'),code=el.querySelector('[data-streamdown="code-block"]');
   return {font:style(el).fontSize,line:style(el).lineHeight,heading:style(el.querySelector('h2')).fontSize,tableBorder:style(table).borderTopWidth,tableInnerBorder:style(table.lastElementChild).borderTopWidth,tableHeaderBackground:style(el.querySelector('thead')).backgroundColor,codeOuterBorder:style(code).borderTopWidth,codeHeader:style(code.querySelector('[data-streamdown="code-block-header"]')).display,codeBackground:style(code).backgroundColor,codeShadow:style(code).boxShadow,codeInnerBorder:style(code.querySelector('[data-streamdown="code-block-body"]')).borderTopWidth,codeActionsBorder:style(code.querySelector('[data-streamdown="code-block-actions"]')).borderTopWidth,blockquoteBackground:style(el.querySelector('blockquote')).backgroundColor,math:el.querySelectorAll('.katex').length,diagramNodes:el.querySelectorAll('[data-streamdown="mermaid-block"] svg g.node').length};
  });
  assert(metrics.tableBorder==='0px'&&metrics.tableInnerBorder==='0px'&&metrics.tableHeaderBackground==='rgba(0, 0, 0, 0)','table still uses a card');
  assert(metrics.codeOuterBorder==='0px'&&metrics.codeHeader==='none'&&metrics.codeShadow==='none'&&metrics.codeInnerBorder==='1px'&&metrics.codeActionsBorder==='0px','code still has layered card chrome');
  await screenshot('light-top','理解注意力中的信息交换');await screenshot('light-code','一个简洁的实现');await screenshot('light-diagram','查看数据流');
  const copyButton=code.getByRole('button',{name:'复制代码',exact:true});const normalCopy=await copyButton.evaluate(el=>({color:getComputedStyle(el).color,background:getComputedStyle(el).backgroundColor}));await copyButton.hover();await p.waitForTimeout(180);const hoveredCopy=await copyButton.evaluate(el=>({color:getComputedStyle(el).color,background:getComputedStyle(el).backgroundColor}));assert(normalCopy.color!==hoveredCopy.color||normalCopy.background!==hoveredCopy.background,'copy hover has no emphasis');
  await copyButton.click();const copiedCode=await p.evaluate(()=>navigator.clipboard.readText());assert(copiedCode.includes('def self_attention'),'code copy failed');
  assert(await p.getByRole('button',{name:'复制表格',exact:true}).count()===0,'table copy controls remain visible');
  await p.evaluate(()=>document.documentElement.classList.add('dark'));await screenshot('dark-top','理解注意力中的信息交换');await screenshot('dark-code','一个简洁的实现');await screenshot('dark-diagram','查看数据流');
  await p.setViewportSize({width:390,height:844});await screenshot('mobile-dark','两种注意力方式');
  const overflowDark=await p.evaluate(()=>document.documentElement.scrollWidth>innerWidth);assert(!overflowDark,'dark mobile page overflows');
  const tableScroll=await p.locator('[data-streamdown="table-wrapper"] > div').last().evaluate(el=>({width:el.clientWidth,scrollWidth:el.scrollWidth,before:el.scrollLeft,after:(el.scrollLeft=120)}));assert(tableScroll.scrollWidth>tableScroll.width&&tableScroll.after>0,'mobile table does not scroll internally');
  await p.evaluate(()=>document.documentElement.classList.remove('dark'));await screenshot('mobile-light','一个简洁的实现');assert(!await p.evaluate(()=>document.documentElement.scrollWidth>innerWidth),'light mobile page overflows');
  await p.setViewportSize({width:1440,height:1050});
  streamText='# 流式排版\n\n中文内容会轻柔出现，已经完成的段落保持稳定。\n\n**强调正在继续';record.messages[1].id='stream-job';record.messages[1].content=streamText;record.generation={id:'stream-job',status:'running',sequence:0};record.version++;
  await p.reload();const streamHeading=p.getByRole('heading',{name:'流式排版',exact:true});await streamHeading.waitFor();const stableHeading=await streamHeading.elementHandle();
  await p.locator('.markdown-body[data-streaming=true] [data-sd-animate]').first().waitFor();
  streamText+='并自动闭合**。\n\n'+String.fromCharCode(96).repeat(3)+'typescript\nconst token = "正在生成"\n';record.messages[1].content=streamText;
  const streamingCode=p.locator('[data-streamdown="code-block"]').filter({hasText:'const token'});await streamingCode.waitFor({timeout:15000});assert(await streamingCode.getByRole('button',{name:'复制代码',exact:true}).isDisabled(),'incomplete code controls remain active');
  assert(await streamHeading.evaluate((el,previous)=>el===previous,stableHeading),'completed heading remounted during streaming');
  await p.emulateMedia({reducedMotion:'reduce'});const reduced=await p.locator('[data-sd-animate]').first().evaluate(el=>getComputedStyle(el).animationName);assert(reduced==='none','reduced motion ignored');
  streamText+='\n'+String.fromCharCode(96).repeat(3)+'\n\n| 项目 | 状态 |\n|---|---|\n| 标题 | **稳定';record.messages[1].content=streamText;await p.getByRole('columnheader',{name:'状态',exact:true}).waitFor({timeout:15000});
  await p.screenshot({path:'/data/wenbo/services/sub2api/output/playwright/chat-markdown-'+mode+'-streaming.png',fullPage:true});
  streamText+='** |\n';record.messages[1].content=streamText;record.generation.status='completed';record.version++;
  await p.locator('.markdown-body[data-streaming=true]').waitFor({state:'detached',timeout:15000});assert(await p.locator('[data-sd-animate]').count()===0,'completed reply keeps animation wrappers');assert(errors.length===0,errors.join('; '));
  return{mode,metrics,codeCopied:true,copyHoverEmphasis:true,tableControlsHidden:true,mobileTableScroll:tableScroll,noPageOverflow:true,streamingIncompleteBlocks:true,stableHeading:true,reducedMotion:true,completedAnimationWrappers:0,errors,entry:await p.locator('script[type=module]').last().getAttribute('src')};
 } finally {await c.close();}
}
