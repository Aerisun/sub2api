#!/usr/bin/env node
'use strict';

// VS Code remote hosts omit partial messages even when the API uses SSE.
// The supported process-wrapper setting supplies the bundled CLI as argv[0].
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawn } = require('node:child_process');
const { createHash } = require('node:crypto');
const windowsHashes = {
  x64: '1803f7adb1884fa7aa514f8c8be668ded726688d0a0cde95f0a3abdf90e8c830',
  arm64: '16dec05f0b2bfe86df36474bc2ccc5983caec9d08d6398c46ce5a3563854e889'
};

function streamingArgs(args, initialEffort = '') {
  const streaming = args.some((arg, i) => arg === '--output-format=stream-json'
    || (arg === '--output-format' && args[i + 1] === 'stream-json'));
  let result = streaming && !args.includes('--include-partial-messages')
    ? [...args, '--include-partial-messages'] : args;
  if (streaming && initialEffort === 'max' && !args.some(arg => arg === '--effort' || arg.startsWith('--effort='))) {
    result = [...result, '--effort', 'max'];
  }
  return result;
}

// Token offsets let us change one top-level setting without removing JSONC
// comments, trailing commas, unrelated settings, or the user's formatting.
function jsonTokens(text) {
  const tokens = [];
  let i = text.charCodeAt(0) === 0xfeff ? 1 : 0;
  while (i < text.length) {
    if (/\s/.test(text[i])) { i++; continue; }
    if (text.slice(i, i + 2) === '//') {
      const end = text.indexOf('\n', i + 2); i = end < 0 ? text.length : end; continue;
    }
    if (text.slice(i, i + 2) === '/*') {
      const end = text.indexOf('*/', i + 2);
      if (end < 0) throw Error('Unterminated settings comment');
      i = end + 2; continue;
    }
    const start = i;
    if (text[i] === '"') {
      i++;
      while (i < text.length && text[i] !== '"') {
        i += text[i] === '\\' ? 2 : 1;
      }
      if (i >= text.length) throw Error('Unterminated settings string');
      i++;
    } else if ('{}[],:'.includes(text[i])) {
      i++;
    } else {
      const literal = text.slice(i).match(/^(?:true|false|null|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)/);
      if (!literal) throw Error('Invalid settings token');
      i += literal[0].length;
    }
    tokens.push({start, end: i, raw: text.slice(start, i)});
  }
  return tokens;
}

function validateSettings(tokens) {
  const json = tokens.filter((t, i) => !(t.raw === ',' && ['}', ']'].includes(tokens[i + 1]?.raw)))
    .map(t => t.raw).join('');
  const parsed = JSON.parse(json);
  if (!parsed || Array.isArray(parsed) || typeof parsed !== 'object') throw Error('Settings must be an object');
}

function patchSettings(text, executable) {
  const tokens = jsonTokens(text);
  validateSettings(tokens);
  const setting = 'claudeCode.claudeProcessWrapper';
  let depth = 0;
  for (let i = 0; i < tokens.length; i++) {
    const token = tokens[i];
    if (token.raw === '{' || token.raw === '[') { depth++; continue; }
    if (token.raw === '}' || token.raw === ']') { depth--; continue; }
    if (depth === 1 && token.raw.startsWith('"') && tokens[i + 1]?.raw === ':'
        && JSON.parse(token.raw) === setting) {
      const value = tokens[i + 2];
      if (!value.raw.startsWith('"')) throw Error('Existing process wrapper must be a string');
      const previous = JSON.parse(value.raw);
      if (previous && previous !== executable) {
        throw Error('An existing Claude process wrapper is configured; combine its behavior before installing');
      }
      return text.slice(0, value.start) + JSON.stringify(executable) + text.slice(value.end);
    }
  }
  const newline = text.includes('\r\n') ? '\r\n' : '\n';
  const insertion = `${newline}  ${JSON.stringify(setting)}: ${JSON.stringify(executable)}${tokens[1].raw === '}' ? '' : ','}`;
  return text.slice(0, tokens[0].end) + insertion + text.slice(tokens[0].end);
}

function defaultSettingsPaths() {
  const home = os.homedir();
  const desktop = process.platform === 'win32'
    ? path.join(process.env.APPDATA || path.join(home, 'AppData', 'Roaming'), 'Code', 'User', 'settings.json')
    : process.platform === 'darwin'
      ? path.join(home, 'Library', 'Application Support', 'Code', 'User', 'settings.json')
      : path.join(process.env.XDG_CONFIG_HOME || path.join(home, '.config'), 'Code', 'User', 'settings.json');
  const candidates = [desktop,
    path.join(home, '.vscode-server', 'data', 'Machine', 'settings.json'),
    path.join(home, '.vscode-server-insiders', 'data', 'Machine', 'settings.json')];
  if (process.env.VSCODE_AGENT_FOLDER) {
    candidates.push(path.join(process.env.VSCODE_AGENT_FOLDER, 'data', 'Machine', 'settings.json'));
  }
  const existing = [...new Set(candidates)].filter(p => fs.existsSync(p));
  return existing.length ? existing : [desktop];
}

async function install(args) {
  const value = flag => {
    const i = args.indexOf(flag);
    if (i < 0) return undefined;
    if (!args[i + 1] || args[i + 1].startsWith('--')) throw Error(`Missing value for ${flag}`);
    return args[i + 1];
  };
  const directory = path.resolve(value('--install-dir') || path.join(os.homedir(), '.claude', 'streaming'));
  const settings = value('--settings');
  const files = settings ? [path.resolve(settings)] : defaultSettingsPaths();
  const script = path.join(directory, 'claude-stream-wrapper.cjs');
  const executable = path.join(directory, process.platform === 'win32'
    ? 'claude-stream-wrapper.exe' : 'claude-stream-wrapper.sh');
  // Validate every intended edit before writing any setting.
  const edits = files.map(file => {
    const original = fs.existsSync(file) ? fs.readFileSync(file, 'utf8') : '{}\n';
    return {file, original, updated: patchSettings(original, executable)};
  });
  let windowsBinary;
  if (process.platform === 'win32') {
    const expectedHash = windowsHashes[process.arch];
    if (!expectedHash || expectedHash === 'PENDING') throw Error('This Windows architecture is not supported');
    const base = value('--download-base') || 'https://aerisun.top/sub2api/client-streaming';
    const response = await fetch(`${base.replace(/\/$/, '')}/claude-stream-wrapper-windows-${process.arch}.exe`);
    if (!response.ok) throw Error(`Launcher download failed: HTTP ${response.status}`);
    windowsBinary = Buffer.from(await response.arrayBuffer());
    if (createHash('sha256').update(windowsBinary).digest('hex') !== expectedHash) {
      throw Error('Launcher checksum does not match; settings were not changed');
    }
  }
  fs.mkdirSync(directory, {recursive: true, mode: 0o700});
  const source = fs.readFileSync(__filename);
  fs.writeFileSync(script, source, {mode: 0o700});
  fs.chmodSync(script, 0o700);
  if (windowsBinary) fs.writeFileSync(executable, windowsBinary);
  else {
    // GUI extension hosts may not inherit the terminal's Node PATH.
    const quote = text => `'${text.replace(/'/g, `'"'"'`)}'`;
    fs.writeFileSync(executable, `#!/bin/sh\nexec ${quote(process.execPath)} ${quote(script)} "$@"\n`, {mode: 0o700});
    fs.chmodSync(executable, 0o700);
  }
  const stamp = new Date().toISOString().replace(/[:.]/g, '-');
  for (const edit of edits) {
    fs.mkdirSync(path.dirname(edit.file), {recursive: true});
    if (fs.existsSync(edit.file)) fs.copyFileSync(edit.file, `${edit.file}.streaming-backup-${stamp}`);
    const temp = `${edit.file}.streaming-${process.pid}.tmp`;
    fs.writeFileSync(temp, edit.updated, {mode: fs.existsSync(edit.file) ? fs.statSync(edit.file).mode & 0o777 : 0o600});
    fs.renameSync(temp, edit.file);
    console.log(`已启用 Claude Code 流式显示：${edit.file}`);
  }
  console.log('在 VS Code 执行 Developer: Reload Window，然后新建会话。');
}

function launch(args) {
  const [binary, ...rest] = args;
  if (!binary) throw Error('Expected the bundled Claude executable as the first argument');
  const environment = {...process.env};
  const initialEffort = environment.AERISUN_CLAUDE_INITIAL_EFFORT || '';
  delete environment.AERISUN_CLAUDE_INITIAL_EFFORT;
  const child = spawn(binary, streamingArgs(rest, initialEffort), {stdio: 'inherit', windowsHide: true, env: environment});
  for (const signal of ['SIGINT', 'SIGTERM']) process.on(signal, () => child.kill(signal));
  child.on('error', error => { console.error(error.message); process.exitCode = 1; });
  child.on('exit', (code, signal) => { process.exitCode = code ?? (signal === 'SIGINT' ? 130 : 143); });
}

module.exports = {streamingArgs, patchSettings};
if (require.main === module) {
  (async () => {
    const args = process.argv.slice(2);
    if (args[0] === '--install') await install(args.slice(1));
    else launch(args);
  })().catch(error => { console.error(error.message); process.exitCode = 1; });
}
