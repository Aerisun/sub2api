/* Parameters stay in this page; API keys are loaded from the signed-in user's account. */
(() => {
  document.addEventListener('DOMContentLoaded', () => {
    const placeholderKey = 'sk-REPLACE_WITH_YOUR_API_KEY';
    const apiBase = '/sub2api/api/v1';
    const builders = [];
    let keys = [];
    let selectedKeyId = '';
    let keyState = 'loading';
    let loadedToken = null;
    let keyRequest = null;
    let requestVersion = 0;
    const fields = {};
    const contextLimits = JSON.parse(document.getElementById('model-context-limits')?.textContent || '{}');
    // The native client reserves output/compaction headroom within this model window.
    const compactSettings = id => contextLimits[id]
      ? {autoCompactWindow: Math.min(1000000, contextLimits[id])} : {};
    let refresh;

    const bashString = value => "'" + value.replace(/'/g, "'\"'\"'") + "'";
    const powershellString = value => "'" + value.replace(/'/g, "''") + "'";
    const jsonString = value => JSON.stringify(value);
    const hasEffort = model => model.startsWith('claude-') || (model.startsWith('gemini-') && !model.includes('image'))
      || model.startsWith('gpt-6') || model.startsWith('gpt-5.6')
      || ['kimi-k3', 'kimi-k3-free', 'deepseek-v4.1-flash'].includes(model)
      || model.toLowerCase().startsWith('nemotron-') || model.startsWith('step-');
    function authToken() {
      try { return localStorage.getItem('auth_token'); } catch { return null; }
    }
    function usableKey(key) {
      return key.status === 'active' && (!key.expires_at || Date.parse(key.expires_at) > Date.now())
        && (!key.quota || key.quota_used < key.quota) && typeof key.key === 'string'
        && /^[\x21-\x7e]+$/.test(key.key) && key.key !== placeholderKey;
    }

    const toolPrompts = new Map([['image_generation','画一只坐在窗边的橘猫'],['image_recognition','描述图片并读出文字']]);
    let previousTool = 'image_generation';

    function renderToolCode(builder) {
      const tool = fields.tool.value;
      if (tool !== previousTool) {
        if (previousTool !== 'web_search') toolPrompts.set(previousTool, fields.imagePrompt.value);
        if (tool !== 'web_search') fields.imagePrompt.value = toolPrompts.get(tool) || '';
        previousTool = tool;
      }
      const prompt = fields.imagePrompt.value;
      const image = fields.toolImage.value.trim() || 'image.png';
      const query = fields.searchWords.value;
      const taskId = fields.imageTask.value.trim() || 'imgtask_替换为返回的任务编号';
      const imageSize = fields.imageSize.value;
      const key = keys.find(item => String(item.id) === selectedKeyId && usableKey(item))?.key || placeholderKey;
      const base = 'https://aerisun.top/sub2api/v1/tools/';
      function curl(name, body, idempotent = false) {
        return 'curl -sS ' + base + name + ' \\\n  -H "Authorization: Bearer $SUB2API_API_KEY" \\\n  -H \'Content-Type: application/json\' \\\n' + (idempotent ? '  -H "Idempotency-Key: image-$(date +%s)-$$" \\\n' : '') + '  -d ' + bashString(JSON.stringify(body));
      }
      function powershell(name, assignments, idempotent = false, prelude = '') {
        return prelude + '$body = @{\n' + assignments.map(([name, value]) => '  ' + name + ' = ' + value).join('\n')
          + '\n} | ConvertTo-Json -Depth 6\nInvoke-RestMethod -Method Post -Uri ' + powershellString(base + name)
          + ' `\n  -Headers @{ Authorization = "Bearer $env:SUB2API_API_KEY"' + (idempotent ? '; "Idempotency-Key" = [guid]::NewGuid().ToString()' : '')
          + ' } `\n  -ContentType \'application/json; charset=utf-8\' `\n  -Body ([System.Text.Encoding]::UTF8.GetBytes($body)) | ConvertTo-Json -Depth 20';
      }
      const gen = { model: fields.imageModel.value, prompt, size:'1024x1024', imageSize, n:1 };
      const poll = { task_id:taskId, wait_seconds:30 };
      const remote = /^https?:\/\//i.test(image) || image.startsWith('data:');
      let recognizeLinux = curl('image_recognition', { image, prompt });
      let recognizeWindows = powershell('image_recognition', [['image',powershellString(image)],['prompt',powershellString(prompt)]]);
      if (!remote) {
        recognizeLinux = 'curl -sS ' + base + 'image_recognition \\\n  -H "Authorization: Bearer $SUB2API_API_KEY" \\\n  -F ' + bashString('image=@' + image) + ' \\\n  --form-string ' + bashString('prompt=' + prompt);
        recognizeWindows = 'curl.exe -sS ' + base + 'image_recognition `\n  -H "Authorization: Bearer $env:SUB2API_API_KEY" `\n  -F ' + powershellString('image=@' + image) + ' `\n  --form-string ' + powershellString('prompt=' + prompt);
      }
      const outputs = {
        'tools-key-code': 'export SUB2API_API_KEY=' + bashString(key),
        'tools-key-windows-code': '$env:SUB2API_API_KEY = ' + powershellString(key),
        'image-generation-code': curl('image_generation', gen, true),
        'image-generation-windows-code': powershell('image_generation', [['model',powershellString(fields.imageModel.value)],['prompt',powershellString(prompt)],['size',"'1024x1024'"],['imageSize',powershellString(imageSize)],['n','1']], true),
        'image-poll-code': curl('image_generation', poll),
        'image-poll-windows-code': powershell('image_generation', [['task_id',powershellString(taskId)],['wait_seconds','30']]),
        'vision-code': recognizeLinux,
        'vision-windows-code': recognizeWindows,
        'tools-search-code': curl('web_search', { query, max_results:20 }),
        'tools-search-windows-code': powershell('web_search', [['query',powershellString(query)],['max_results','20']]),
      };
      for (const [code] of builder.templates) {
        code.textContent = outputs[code.id] ?? code.textContent;
        code.dataset.requiresKey = String(code.textContent.includes(placeholderKey));
        code.dataset.keyReady = String(key !== placeholderKey);
      }
      for (const panel of builder.section.querySelectorAll('[data-tool-panel]')) panel.hidden = panel.dataset.toolPanel !== tool;
    }

    function renderCode(builder) {
      const { section, templates } = builder;
      if (section.id === 'vision') { renderToolCode(builder); return; }
      const model = section.id === 'embedding' ? fields.embeddingModel.value
        : (fields.model.value === 'custom' ? fields.customModel.value.trim() : fields.model.value) || builder.defaultModel;
      const content = fields.content?.value ?? '';
      const imagePath = fields.image?.value.trim() || 'image.png';
      const key = keys.find(item => String(item.id) === selectedKeyId && usableKey(item))?.key || placeholderKey;
      const selectedEffort = hasEffort(model) ? fields.effort?.value : '';
      const effort = model.startsWith('gpt-6') || model.startsWith('gpt-5.6')
        ? (selectedEffort && selectedEffort !== 'auto' ? selectedEffort : 'medium') : selectedEffort;
      const geminiEnvironment = hasEffort(model) ? {
        ANTHROPIC_CUSTOM_MODEL_OPTION: model,
        CLAUDE_CODE_EFFORT_LEVEL: effort || 'low',
        ANTHROPIC_CUSTOM_MODEL_OPTION_SUPPORTED_CAPABILITIES: 'effort,thinking,adaptive_thinking',
        ANTHROPIC_DEFAULT_OPUS_MODEL_SUPPORTED_CAPABILITIES: 'effort,thinking,adaptive_thinking',
        ANTHROPIC_DEFAULT_SONNET_MODEL_SUPPORTED_CAPABILITIES: 'effort,thinking,adaptive_thinking',
        ANTHROPIC_DEFAULT_HAIKU_MODEL_SUPPORTED_CAPABILITIES: 'effort,thinking,adaptive_thinking',
      } : {};
      const claudeEnvironment = {
        ANTHROPIC_API_KEY: '',
        CLAUDE_CODE_OAUTH_TOKEN: '',
        ANTHROPIC_CUSTOM_HEADERS: '',
        CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY: '1',
        DISABLE_AUTO_COMPACT: '',
        DISABLE_COMPACT: '',
        CLAUDE_CODE_AUTO_COMPACT_WINDOW: '',
        ANTHROPIC_CUSTOM_MODEL_OPTION: model,
        ANTHROPIC_CUSTOM_MODEL_OPTION_NAME: model,
        NO_PROXY: 'aerisun.top,localhost,127.0.0.1,::1',
        no_proxy: 'aerisun.top,localhost,127.0.0.1,::1',
        ...geminiEnvironment,
        // A fixed environment effort overrides the native slider for the whole session.
        CLAUDE_CODE_EFFORT_LEVEL: '',
        AERISUN_CLAUDE_INITIAL_EFFORT: effort === 'max' ? 'max' : '',
      };
      const modelPicker = {
        replaceBuiltInOptions: true,
        options: [...new Set([...fields.model.options].map(option => option.value))]
          .filter(id => id !== 'custom' && !id.includes('embedding') && !id.includes('reranker') && !id.includes('image'))
          .map(id => ({model: id, label: id, ...(id.startsWith('claude-') ? {} : {behavesAs: hasEffort(id) ? 'claude-sonnet-5' : 'claude-sonnet-4-6'})})),
      };
      const texts = content.split(/\r?\n/).filter(line => line.trim());
      for (const [code, template] of templates) {
        const windows = code.closest('[data-command-panel]')?.dataset.commandPanel === 'windows';
        const quote = windows ? powershellString : bashString;
        let result = template.replace(/'(?:你的本站密钥|sk-REPLACE_WITH_YOUR_API_KEY)'/g, () => quote(key))
          .replace(/"(?:你的本站密钥|sk-REPLACE_WITH_YOUR_API_KEY)"/g, () => jsonString(key));
        if (section.id === 'direct') {
          if (windows) {
            result = result.replace(/model = '[^']*'/, () => 'model = ' + quote(model))
              .replace(/content = '[^']*'/, () => 'content = ' + quote(content));
          } else {
            const body = JSON.stringify({ model, ...(effort ? {reasoning_effort: effort} : {}),
              messages: [{role: 'user', content}] }, null, 2);
            result = result.replace(/-d '[\s\S]*'$/, () => '-d ' + quote(body));
          }
        } else if (section.id === 'embedding') {
          if (code.id.startsWith('rerank-')) {
            if (windows) {
              result = result.replace(/query = '[^']*'/, () => 'query = ' + quote(content));
            } else {
              const body = JSON.parse(template.match(/-d '([\s\S]*)'$/)[1]);
              body.query = content;
              result = result.replace(/-d '[\s\S]*'$/, () => '-d ' + quote(JSON.stringify(body, null, 2)));
            }
          } else if (code.id === 'embedding-code') {
            const body = JSON.stringify({ model, input: texts, encoding_format: 'float' });
            result = result.replace(/-d '[\s\S]*'$/, () => '-d ' + quote(body));
          } else if (windows) {
            result = result.replace(/model = '[^']*'/, () => 'model = ' + quote(model))
              .replace(/input = @\([^\n]*\)/, () => 'input = @(' + texts.map(quote).join(', ') + ')');
          } else if (code.id === 'embedding-python-code') {
            result = result.replace(/"model": "qwen3-embedding-4b"/, () => '"model": ' + jsonString(model))
              .replace(/"input": \[[^\n]*\]/, () => '"input": [' + texts.map(jsonString).join(', ') + ']')
              .replace(/"query": "[^"\n]*"/, () => '"query": ' + jsonString(content));
          } else {
            result = result.replace(/model="[^"]*"/, () => 'model=' + jsonString(model))
              .replace(/input=\[[^\n]*\]/, () => 'input=[' + texts.map(jsonString).join(', ') + ']')
              .replace('api_key=os.environ["SUB2API_API_KEY"]', () => 'api_key=' + jsonString(key));
          }
        } else if (template.trimStart().startsWith('{')) {
          const config = JSON.parse(template);
          if (config.env) {
            for (const name of Object.keys(config.env)) {
              if (name === 'ANTHROPIC_AUTH_TOKEN') config.env[name] = key;
              if (/^(ANTHROPIC_(MODEL|DEFAULT_.*_MODEL)|CLAUDE_CODE_SUBAGENT_MODEL)$/.test(name)) config.env[name] = model;
            }
            if (effort) config.env.CLAUDE_CODE_EFFORT_LEVEL = effort;
            Object.assign(config.env, geminiEnvironment);
            if (section.id === 'claude' || section.id === 'vscode') {
              Object.assign(config.env, claudeEnvironment);
              config.model = model;
              config.autoCompactEnabled = true;
              delete config.autoCompactWindow;
              config.modelPicker = modelPicker;
              config.modelSettings = Object.fromEntries(modelPicker.options
                .map(option => [option.model, {...compactSettings(option.model), ...(hasEffort(option.model) ? {
                  effortLevel: option.model === model && ['low', 'medium', 'high', 'xhigh'].includes(effort) ? effort : 'low', maxEffortLevel: 'max'} : {})}]));
            }
          }
          if (section.id === 'vscode') {
            for (const variable of config['claudeCode.environmentVariables'] || []) {
              if (variable.name === 'ANTHROPIC_AUTH_TOKEN') variable.value = key;
              if (/^(ANTHROPIC_(MODEL|DEFAULT_.*_MODEL)|CLAUDE_CODE_SUBAGENT_MODEL)$/.test(variable.name)) variable.value = model;
            }
            if (config['claudeCode.environmentVariables']) {
              delete config['claudeCode.selectedModel'];
              config['claudeCode.environmentVariables'].push(...Object.entries(claudeEnvironment).map(([name, value]) => ({name, value})));
              for (const variable of config['claudeCode.environmentVariables']) {
                if (variable.name === 'CLAUDE_CODE_EFFORT_LEVEL') variable.value = claudeEnvironment.CLAUDE_CODE_EFFORT_LEVEL;
              }
            }
          }
          if (section.id === 'opencode') {
            config.model = 'aerisun/' + model;
            const selected = config.provider?.aerisun?.models?.[model];
            if (selected && hasEffort(model)) selected.options = {...selected.options, reasoningEffort: effort || 'low'};
          }
          result = JSON.stringify(config, null, 2);
        } else {
          result = result.replace(/((?:export |\$env:)(?:ANTHROPIC_(?:MODEL|DEFAULT_\w+_MODEL)|CLAUDE_CODE_SUBAGENT_MODEL)\s*=\s*)'[^']*'/g,
            (_, prefix) => prefix + quote(model));
          if (section.id === 'python' || (section.id === 'vision' && code.id !== 'image-generation-code')) {
            result = result.replace(/model="[^"]*"/, () => 'model=' + jsonString(model))
              .replace(/image_path = Path\("[^"]*"\)/, () => 'image_path = Path(' + jsonString(imagePath) + ')');
            result = result.replace(/"(content|text)": "[^"\n]*"/g, (_, name) => jsonString(name) + ': ' + jsonString(content));
            // Each downloadable Python example uses the key selected in its own section.
            result = result.replace('api_key=os.environ["SUB2API_API_KEY"]', () => 'api_key=' + jsonString(key));
          }
          if (section.id === 'codex') {
            result = result.replace(/^model = "[^"]*"/m, () => 'model = ' + jsonString(model));
            if (effort) {
              result = result.replace(/^(?:model_reasoning_effort|plan_mode_reasoning_effort) = "[^"]*"$/gm,
                line => line.slice(0, line.indexOf('=')) + '= ' + jsonString(effort));
            } else {
              result = result.replace(/^(?:model_reasoning_effort|plan_mode_reasoning_effort) = "[^"]*"\n?/gm, '');
            }
            if (/^codex(?: --model [^\n]+)?$/.test(template)) {
              result = 'codex --model ' + quote(model) + (content ? ' ' + quote(content) : '');
            }
          }
          if (section.id === 'claude' && content) {
            result = result.replace(/(claude --dangerously-skip-permissions)$/, () => 'claude --dangerously-skip-permissions ' + quote(content));
          }
          if (section.id === 'vision' && code.id.startsWith('codex-image-')) {
            result = (windows ? '$env:SUB2API_API_KEY = ' : 'export SUB2API_API_KEY=') + quote(key)
              + '\n\ncodex --model ' + quote(model) + ' --image ' + quote(imagePath) + ' ' + quote(content);
          }
          if (section.id === 'vision' && code.id === 'image-generation-code') {
            result = result.replace(/prompt="[^"\n]*"/, () => 'prompt=' + jsonString(content));
          }
        }
        if (effort && section.id === 'direct' && windows) {
          result = result.replace(/(model = '[^']*')/, '$1\n  reasoning_effort = ' + quote(effort));
        }
        if (effort && section.id === 'python') {
          result = result.replace(/(model="[^"]*",)/, '$1\n    reasoning_effort=' + jsonString(effort) + ',');
        }
        if (section.id === 'claude' && ['claude-code', 'claude-windows-code'].includes(code.id)) {
          if (hasEffort(model)) {
            const initialEffort = ['low', 'medium', 'high', 'xhigh', 'max'].includes(effort) ? effort : 'low';
            result = result.replace('claude --dangerously-skip-permissions', 'claude --effort ' + initialEffort + ' --dangerously-skip-permissions');
          }
          const variables = Object.entries(claudeEnvironment).map(([name, value]) =>
            (windows ? '$env:' + name + ' = ' : 'export ' + name + '=') + quote(value)).join('\n');
          if (variables) result = variables + '\n' + result;
        }
        code.textContent = result;
        code.dataset.requiresKey = String(result.includes(placeholderKey) || result.includes('你的本站密钥'));
        code.dataset.keyReady = String(key !== placeholderKey);
      }
    }

    function updateKeys() {
      const usable = keys.filter(usableKey);
      if (!usable.some(key => String(key.id) === selectedKeyId)) selectedKeyId = '';
      if (!selectedKeyId && usable.length === 1) selectedKeyId = String(usable[0].id);
      const labels = {
        loading: '正在加载你的密钥…', login: '登录后可选择已有密钥',
        expired: '登录已过期，请重新登录', error: '密钥加载失败，请重试',
        ready: usable.length ? '选择你的密钥' : '暂无可用密钥',
      };
      const select = fields.key;
      select.replaceChildren(new Option(labels[keyState], ''));
      for (const key of keys) {
        const status = usableKey(key) ? '' : ' · 不可用';
        const masked = key.key ? key.key.slice(0, 6) + '…' + key.key.slice(-4) : '';
        const option = new Option((key.name || '未命名密钥') + ' · ' + masked + status, String(key.id));
        option.disabled = !usableKey(key);
        select.append(option);
      }
      select.value = selectedKeyId;
      select.disabled = keyState !== 'ready' || !usable.length;
      refresh.disabled = keyState === 'loading';
      builders.forEach(renderCode);
    }

    async function loadKeys() {
      const version = ++requestVersion;
      keyRequest?.abort();
      keyRequest = null;
      const token = authToken();
      const previousSelection = token === loadedToken ? selectedKeyId : '';
      loadedToken = token;
      keys = [];
      selectedKeyId = '';
      keyState = token ? 'loading' : 'login';
      updateKeys();
      if (!token) return;
      const controller = new AbortController();
      keyRequest = controller;
      const timeout = setTimeout(() => controller.abort(), 15000);
      try {
        const items = [];
        for (let page = 1; ; page++) {
          const response = await fetch(apiBase + '/keys?page=' + page + '&page_size=100', {
            headers: { Authorization: 'Bearer ' + token }, credentials: 'same-origin', signal: controller.signal,
          });
          if (version !== requestVersion) return;
          if (token !== authToken()) { loadKeys(); return; }
          if (response.status === 401) {
            if (version !== requestVersion) return;
            keyState = 'expired';
            return;
          }
          if (!response.ok) throw new Error('keys');
          const payload = await response.json();
          if (payload.code !== 0 || !Array.isArray(payload.data?.items) || !Number.isFinite(payload.data.total)) throw new Error('keys');
          items.push(...payload.data.items);
          if (!payload.data.items.length || items.length >= payload.data.total) break;
        }
        if (version !== requestVersion) return;
        if (token !== authToken()) { loadKeys(); return; }
        keys = items;
        selectedKeyId = previousSelection;
        keyState = 'ready';
      } catch {
        if (version !== requestVersion) return;
        keyState = 'error';
      } finally {
        clearTimeout(timeout);
        if (version === requestVersion) {
          keyRequest = null;
          updateKeys();
        }
      }
    }

    const sidebar = document.querySelector('.docs-guide-sidebar');
    if (!sidebar) return;
    const catalog = [...document.querySelectorAll('[data-model-id]')].map(button => button.dataset.modelId);
    for (const section of document.querySelectorAll('[data-guide-builder]')) {
      builders.push({ section, defaultModel: 'kimi-k3',
        templates: [...section.querySelectorAll('pre code[id]')].map(code => [code, code.textContent]),
      });
    }
    const form = document.createElement('form');
    form.className = 'docs-builder';
    form.setAttribute('aria-label', '全局配置');
    form.innerHTML = '<div class="docs-builder-fields"></div>';
    const grid = form.firstElementChild;
    function field(name, label, tag) {
      const wrapper = document.createElement('div');
      wrapper.className = 'docs-builder-field';
      const caption = document.createElement('label');
      const control = document.createElement(tag);
      control.id = 'guide-' + name;
      control.name = name;
      control.className = 'input';
      caption.htmlFor = control.id;
      caption.textContent = label;
      wrapper.append(caption, control);
      grid.append(wrapper);
      fields[name] = control;
      return wrapper;
    }
    const modelField = field('model', '模型', 'select');
    for (const group of document.querySelectorAll('.docs-model-group')) {
      const options = document.createElement('optgroup');
      options.label = group.querySelector('h3').textContent;
      for (const button of group.querySelectorAll('[data-model-id]')) {
        const model = button.dataset.modelId;
        if (!model.includes('embedding') && !model.includes('reranker') && !model.includes('image')) options.append(new Option(model, model));
      }
      if (options.children.length) fields.model.append(options);
    }
    fields.model.append(new Option('自定义模型 ID…', 'custom'));
    const codexGuideDefault = location.hash === '#codex' && catalog.includes('gpt-6.1-sol');
    fields.model.value = codexGuideDefault ? 'gpt-6.1-sol'
      : fields.model.options[0].value;
    const customModel = document.createElement('input');
    customModel.className = 'input docs-builder-custom-model';
    customModel.id = 'guide-custom-model';
    customModel.setAttribute('aria-label', '自定义模型 ID');
    customModel.placeholder = '输入模型 ID';
    customModel.autocomplete = 'off';
    customModel.spellcheck = false;
    customModel.hidden = true;
    fields.customModel = customModel;
    modelField.append(customModel);
    const effortField = field('effort', '思考强度', 'select');
    for (const [value, label] of [['low', '低'], ['medium', '中'], ['high', '高'], ['xhigh', '超高'], ['max', '最大']]) {
      fields.effort.append(new Option(label, value));
    }
    fields.effort.value = codexGuideDefault ? 'xhigh' : fields.model.value.startsWith('gpt-') ? 'medium' : 'low';
    effortField.hidden = !hasEffort(fields.model.value);
    const embeddingField = field('embeddingModel', '向量模型', 'select');
    for (const model of catalog.filter(model => model.includes('embedding'))) fields.embeddingModel.append(new Option(model, model));
    if (!fields.embeddingModel.options.length) fields.embeddingModel.append(new Option('qwen3-embedding-4b', 'qwen3-embedding-4b'));
    embeddingField.hidden = true;
    const toolField = field('tool', '工具', 'select');
    for (const [value,label] of [['image_generation','生图'],['image_recognition','识图'],['web_search','联网搜索']]) fields.tool.append(new Option(label,value));
    toolField.hidden = true;
    const keyField = field('key', '选择你的密钥', 'select');
    const actions = document.createElement('div');
    actions.className = 'docs-builder-key-actions';
    const keyLink = document.createElement('a');
    keyLink.href = '/sub2api/keys';
    keyLink.target = '_top';
    keyLink.textContent = '创建密钥';
    refresh = document.createElement('button');
    refresh.type = 'button';
    refresh.textContent = '刷新';
    refresh.addEventListener('click', loadKeys);
    actions.append(keyLink, refresh);
    keyField.append(actions);
    const contentField = field('content', '内容', 'textarea');
    contentField.classList.add('docs-builder-wide');
    fields.content.rows = 3;
    fields.content.value = '你好';
    fields.content.placeholder = '输入内容';
    const imageField = field('image', '图片路径', 'input');
    imageField.classList.add('docs-builder-wide');
    fields.image.value = 'image.png';
    imageField.hidden = true;
    const imageModelField = field('imageModel', '图片模型', 'select');
    for (const model of ['gpt-image-2.5-flare','gpt-image-2.5-sunburst','gpt-image-2']) fields.imageModel.append(new Option(model,model));
    imageModelField.hidden = true;
    const imageSizeField = field('imageSize', '图片规格', 'select');
    for (const value of ['1K','2K','4K']) fields.imageSize.append(new Option(value,value));
    const imagePromptField = field('imagePrompt', '图片提示词', 'textarea');
    fields.imagePrompt.rows = 3;
    fields.imagePrompt.value = '画一只坐在窗边的橘猫';
    const toolImageField = field('toolImage', '图片地址或路径', 'input');
    fields.toolImage.value = 'image.png';
    const imageTaskField = field('imageTask', '生图任务编号', 'input');
    fields.imageTask.placeholder = 'imgtask_…';
    const searchWordsField = field('searchWords', '搜索词', 'textarea');
    fields.searchWords.rows = 3;
    fields.searchWords.value = '今天的科技新闻';
    for (const el of [imageSizeField,imagePromptField,toolImageField,imageTaskField,searchWordsField]) el.hidden = true;
    sidebar.append(form);
    const modelEfforts = new Map([[fields.model.value, fields.effort.value]]);
    let previousModel = fields.model.value;
    const renderAll = () => {
      const model = fields.model.value === 'custom' ? fields.customModel.value.trim() : fields.model.value;
      if (model !== previousModel) {
        modelEfforts.set(previousModel, fields.effort.value);
        const defaultEffort = model === 'gpt-6.1-sol' && location.hash === '#codex' ? 'xhigh'
          : model.startsWith('gpt-') ? 'medium' : 'low';
        fields.effort.value = modelEfforts.get(model) || defaultEffort;
        previousModel = model;
      }
      effortField.hidden = !hasEffort(model);
      builders.forEach(renderCode);
      updateActiveSection();
    };
    form.addEventListener('submit', event => event.preventDefault());
    form.addEventListener('input', event => { if (event.target !== fields.key) renderAll(); });
    fields.model.addEventListener('change', () => {
      fields.customModel.hidden = fields.model.value !== 'custom';
      effortField.hidden = !hasEffort(fields.model.value);
      if (!fields.customModel.hidden) fields.customModel.focus();
      renderAll();
    });
    fields.key.addEventListener('change', () => {
      selectedKeyId = fields.key.value;
      updateKeys();
    });
    const sections = [...document.querySelectorAll('.docs-guide-section')];
    const links = [...sidebar.querySelectorAll('.docs-sidenav a')];
    function updateActiveSection() {
      let active = sections[0];
      for (const section of sections) {
        if (section.getBoundingClientRect().top <= 100) active = section;
      }
      const toolsSection = sections.find(section => section.id === 'vision');
      const toolsRect = toolsSection?.getBoundingClientRect();
      if (location.hash === '#vision' && toolsSection && toolsRect && toolsRect.top < innerHeight && toolsRect.bottom > 0) active = toolsSection;
      for (const link of links) {
        if (link.hash === '#' + active.id) link.setAttribute('aria-current', 'location');
        else link.removeAttribute('aria-current');
      }
      const toolsActive = active.id === 'vision';
      toolField.hidden = !toolsActive;
      imageModelField.hidden = !toolsActive || fields.tool.value !== 'image_generation';
      imageSizeField.hidden = !toolsActive || fields.tool.value !== 'image_generation';
      imagePromptField.hidden = !toolsActive || fields.tool.value === 'web_search';
      toolImageField.hidden = !toolsActive || fields.tool.value !== 'image_recognition';
      imageTaskField.hidden = !toolsActive || fields.tool.value !== 'image_generation';
      searchWordsField.hidden = !toolsActive || fields.tool.value !== 'web_search';
      modelField.hidden = ['embedding', 'rerank', 'vision'].includes(active.id);
      contentField.hidden = active.id === 'rerank' || active.id === 'vision';
      const currentModel = fields.model.value === 'custom' ? fields.customModel.value.trim() : fields.model.value;
      effortField.hidden = ['rerank', 'embedding', 'vision'].includes(active.id) || !hasEffort(currentModel);
      embeddingField.hidden = active.id !== 'embedding';
      imageField.hidden = true;
      contentField.querySelector('label').textContent = active.id === 'embedding' ? '文本（每行一条）' : '内容';
    }
    let scrollFrame;
    window.addEventListener('scroll', () => {
      if (scrollFrame) return;
      scrollFrame = requestAnimationFrame(() => { updateActiveSection(); scrollFrame = null; });
    }, { passive: true });
    updateActiveSection();
    renderAll();
    loadKeys();
    window.addEventListener('storage', event => {
      if (event.key === 'auth_token' || event.key === 'auth_user' || event.key === null) loadKeys();
    });
    window.addEventListener('focus', () => { if (!keyRequest && authToken() !== loadedToken) loadKeys(); });
    document.addEventListener('visibilitychange', () => {
      if (!document.hidden && (authToken() !== loadedToken || keyState !== 'ready')) loadKeys();
    });
  });
})();
