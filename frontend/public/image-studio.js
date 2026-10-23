(() => {
  'use strict';
  const BASE = location.pathname.startsWith('/sub2api/') ? '/sub2api' : '';
  const $ = id => document.getElementById(id);
  const tasks = new Map();
  const keys = new Map();
  const emptyState = $('tasks').firstElementChild.cloneNode(true);
  let timer, polling = false;
  const labels = {processing: '生成中', completed: '已完成', failed: '生成失败', interrupted: '任务中断', unavailable: '等待重新查询'};
  const message = text => { $('form-message').textContent = text; };
  const activeKey = () => keys.get($('key-select').value) || $('api-key').value.trim();
  async function jsonRequest(path, key, body, extra = {}) {
    const response = await fetch(BASE + path, {method: body ? 'POST' : 'GET', headers: {'Authorization': 'Bearer ' + key, ...(body ? {'Content-Type': 'application/json'} : {}), ...extra}, body: body ? JSON.stringify(body) : undefined});
    const result = await response.json();
    if (!response.ok) throw new Error(result.error?.message || result.message || '请求失败，请稍后再试');
    return result;
  }
  function node(tag, className, text) { const el = document.createElement(tag); if (className) el.className = className; if (text) el.textContent = text; return el; }
  function render() {
    if (!tasks.size) { $('tasks').replaceChildren(emptyState.cloneNode(true)); return; }
    $('tasks').replaceChildren();
    for (const task of [...tasks.values()].sort((a,b) => b.created_at - a.created_at)) {
      const card = node('article', 'studio-task card');
      const head = node('div', 'studio-task-header');
      const info = node('div');
      info.append(node('p', '', task.prompt || '图片生成任务'), node('p', 'studio-task-id', task.task_id));
      const state = node('span', 'studio-state', labels[task.status] || task.status);
      state.dataset.state = task.status;
      head.append(info, state); card.append(head);
      if (task.status === 'processing') {
        const busy = node('div', 'studio-busy'); busy.append(node('span', 'studio-spinner'), node('span', '', '任务在后台生成，完成后自动显示。')); card.append(busy);
      } else if (task.status === 'completed') {
        const images = node('div', 'studio-images');
        for (const item of task.result?.data || []) {
          const url = item.url || (item.b64_json ? 'data:image/png;base64,' + item.b64_json : '');
          if (!url || !(url.startsWith('https://') || url.startsWith('data:image/'))) continue;
          const figure = node('figure'); const img = node('img'); img.src = url; img.alt = task.prompt || '生成的图片'; img.loading = 'lazy';
          const actions = node('figcaption', 'studio-image-actions');
          const download = node('button', '', '下载图片'); download.type = 'button';
          download.addEventListener('click', async () => { try { const response = await fetch(url); if (!response.ok) throw new Error(); const object = URL.createObjectURL(await response.blob()); const a = node('a'); a.href = object; a.download = task.task_id + (url.endsWith('.jpg') ? '.jpg' : url.endsWith('.webp') ? '.webp' : '.png'); a.click(); setTimeout(() => URL.revokeObjectURL(object), 10000); } catch { window.open(url, '_blank', 'noopener'); } });
          const copy = node('button', '', '复制图片链接'); copy.type = 'button'; copy.addEventListener('click', async () => { try { await navigator.clipboard.writeText(url); copy.textContent = '已复制'; setTimeout(() => copy.textContent = '复制图片链接', 2000); } catch { message('请打开图片后复制地址。'); } });
          const open = node('a', '', '查看原图'); open.href = url; open.target = '_blank'; open.rel = 'noopener noreferrer';
          actions.append(download, copy, open); figure.append(img, actions); images.append(figure);
        }
        card.append(images, node('p', 'studio-hint', `已生成 ${task.image_count || task.result?.data?.length || 1} 张 · ${task.cost == null ? '费用见使用记录' : '$' + Number(task.cost).toFixed(2)}`));
        if (task.image_count < task.pricing?.requested_images) card.append(node('p', 'studio-hint', `请求 ${task.pricing.requested_images} 张，上游实际返回 ${task.image_count} 张，按实际张数计费。`));
      } else {
        card.append(node('p', 'studio-hint', task.error?.message || '生成未完成。请查看使用记录后再决定是否重试。'));
      }
      $('tasks').append(card);
    }
  }
  async function storageName(key = activeKey()) {
    const raw = new TextEncoder().encode(key);
    const hash = await crypto.subtle.digest('SHA-256', raw);
    return 'aerisun-image-tasks:' + Array.from(new Uint8Array(hash)).map(x => x.toString(16).padStart(2,'0')).join('');
  }
  async function saveTask(task, key) {
    try {
      const name = await storageName(key);
      const previous = JSON.parse(localStorage.getItem(name) || '[]').filter(t => t.task_id !== task.task_id);
      previous.push({task_id: task.task_id, created_at: task.created_at});
      localStorage.setItem(name, JSON.stringify(previous.slice(-30)));
    } catch { /* Storage is optional. */ }
  }
  async function poll() {
    clearTimeout(timer);
    if (!activeKey()) return;
    if (polling) { timer = setTimeout(poll, 250); return; }
    polling = true;
    const key = activeKey();
    let pending = false;
    for (const previous of tasks.values()) {
      if (previous.status !== 'processing' && !previous.restore) continue;
      try {
        const response = await jsonRequest('/v1/tools/image_generation', key, {task_id: previous.task_id});
        const task = response.output;
        if (activeKey() !== key) break;
        tasks.set(task.task_id, {...previous, ...task, restore: false, pollErrors: 0});
        if (task.status === 'processing') pending = true;
      } catch (error) {
        if (activeKey() !== key) break;
        previous.pollErrors = (previous.pollErrors || 0) + 1;
        if (previous.pollErrors >= 8) { previous.status = 'unavailable'; previous.restore = false; previous.error = {message: '自动查询已暂停，请点击刷新任务。任务不会因此重新提交。'}; }
        else pending = true;
        message('暂时无法查询任务：' + error.message + '。可稍后点击刷新。');
      }
    }
    polling = false; render();
    if (pending) timer = setTimeout(poll, 4000);
  }
  async function restore() {
    clearTimeout(timer); tasks.clear();
    render();
    const key = activeKey();
    if (!key) return;
    try {
      const name = await storageName(key);
      if (activeKey() !== key) return;
      for (const task of JSON.parse(localStorage.getItem(name) || '[]')) { if (Date.now()/1000 - task.created_at < 7*86400) tasks.set(task.task_id, {...task, status: 'processing', restore: true}); }
    } catch { /* Ignore obsolete client state. */ }
    render(); await poll();
  }
  $('key-select').addEventListener('change', () => { $('api-key').hidden = !!$('key-select').value; restore(); });
  $('api-key').addEventListener('change', restore);
  function updateEstimate() {
    $('model-price').textContent = '按图片尺寸计费';
    $('estimated-cost').textContent = '按实际图片数量与尺寸计费，详见模型广场';
  }
  $('count').addEventListener('change', updateEstimate);

  $('refresh').addEventListener('click', () => { for (const task of tasks.values()) { task.restore = true; task.pollErrors = 0; } poll(); });
  $('image-form').addEventListener('submit', async event => {
    event.preventDefault();
    const key = activeKey();
    if (!key.startsWith('sk-')) { message('请选择或输入本站 API 密钥。'); return; }
    $('generate').disabled = true; message('正在提交任务…');
    try {
      const prompt = $('prompt').value.trim();
      const payload = {model: $('image-model').value, prompt, size: $('size').value, n: Number($('count').value)};
      const file = $('reference').files[0];
      if (file) {
        if (!['image/png','image/jpeg','image/webp'].includes(file.type) || file.size > 8*1024*1024) throw new Error('参考图片须为 PNG、JPEG 或 WebP，且不超过 8 MB。');
        payload.image = [await new Promise((resolve, reject) => { const reader = new FileReader(); reader.onload = () => resolve(reader.result); reader.onerror = reject; reader.readAsDataURL(file); })];
      }
      const response = await jsonRequest('/v1/tools/image_generation', key, payload, {'Idempotency-Key': crypto.randomUUID()});
      const task = response.output;
      await saveTask(task, key);
      if (activeKey() === key) { tasks.set(task.task_id, {...task, prompt}); render(); poll(); }
      message('任务已提交，可以继续浏览，完成后会自动显示。');
      $('reference').value = '';
    } catch (error) { message(error.message || '提交失败，请稍后再试。'); }
    finally { $('generate').disabled = false; }
  });
  (async () => {
    const token = localStorage.getItem('auth_token');
    if (!token) return;
    try {
      const result = await jsonRequest('/api/v1/keys?page_size=100', token);
      const items = result.data?.items || result.items || [];
      for (const key of items) {
        if (key.status !== 'active' || key.group_id !== 9 || (key.allowed_models?.length && !key.allowed_models.some(m => m === 'image_generation' || m.startsWith('gpt-image-')))) continue;
        keys.set(String(key.id), key.key);
        const option = node('option', '', key.name); option.value = String(key.id); $('key-select').append(option);
      }
      if (keys.size) { $('key-select').value = keys.keys().next().value; $('api-key').hidden = true; $('key-hint').firstChild.textContent = '已读取你的本站密钥，密钥不会额外存入浏览器。'; await restore(); }
    } catch { message('登录状态已过期，可手动输入本站密钥或重新登录。'); }
  })();
})();
