'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {spawnSync} = require('node:child_process');
const {streamingArgs, patchSettings} = require('../frontend/client-streaming/claude-stream-wrapper.cjs');

test('helper and non-stream invocations retain their arguments', () => {
  for (const args of [['--version'], ['--output-format', 'json'], ['--claude-in-chrome-mcp']]) {
    assert.deepEqual(streamingArgs(args), args);
  }
  const partial = ['--output-format', 'stream-json', '--include-partial-messages'];
  assert.deepEqual(streamingArgs(partial), partial);
});

test('streaming launches preserve literal arguments and append partial messages', () => {
  const args = ['--output-format', 'stream-json', '--input-format', 'stream-json',
    '--settings', '{"env":{"EXAMPLE":"a b \\" c"}}', '--model', 'glm-5.2'];
  assert.deepEqual(streamingArgs(args), [...args, '--include-partial-messages']);
  assert.deepEqual(streamingArgs(['--output-format=stream-json']),
    ['--output-format=stream-json', '--include-partial-messages']);
});

test('initial max becomes a session flag so the native slider can subsequently lower effort', () => {
  const args=['--output-format','stream-json'];
  assert.deepEqual(streamingArgs(args,'max'),[...args,'--include-partial-messages','--effort','max']);
  assert.deepEqual(streamingArgs(['--version'],'max'),['--version']);
  assert.deepEqual(streamingArgs([...args,'--effort','low'],'max'),[...args,'--effort','low','--include-partial-messages']);
});

test('JSONC comments, trailing commas and nested names survive a top-level insertion', () => {
  const source = '\ufeff{\r\n// keep this comment\r\n"limit":1// adjacent comment\r\n,\r\n'
    + '"nested":{"claudeCode.claudeProcessWrapper":"keep"},\r\n"text":"a,b}",\r\n}\r\n';
  const result = patchSettings(source, '/path with spaces/wrapper.cjs');
  assert.ok(result.includes('// keep this comment'));
  assert.ok(result.includes('1// adjacent comment'));
  assert.ok(result.includes('"nested":{"claudeCode.claudeProcessWrapper":"keep"}'));
  assert.ok(result.includes('"text":"a,b}",\r\n}'));
  assert.ok(result.includes('"claudeCode.claudeProcessWrapper": "/path with spaces/wrapper.cjs",'));
  assert.equal(patchSettings(result, '/path with spaces/wrapper.cjs'), result);
});

test('existing wrappers and malformed settings are never overwritten', () => {
  assert.throws(() => patchSettings('{"claudeCode.claudeProcessWrapper":"/company/wrapper"}', '/new'), /existing Claude/);
  assert.throws(() => patchSettings('{"broken":}', '/new'));
  assert.throws(() => patchSettings('[]', '/new'));
  assert.equal(patchSettings('{/* empty */}', '/new'), '{\n  "claudeCode.claudeProcessWrapper": "/new"/* empty */}');
});

test('installer preserves settings and launches the bundled process with literal arguments', {skip: process.platform === 'win32'}, () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'claude-stream-test-'));
  try {
    const settings = path.join(directory, 'settings.json');
    const source = '// preserve comment\n{"editor.fontSize":14,}\n';
    fs.writeFileSync(settings, source);
    const installDir = path.join(directory, "with spaces and ' quote");
    const installer = path.resolve(__dirname, '../frontend/client-streaming/claude-stream-wrapper.cjs');
    const installed = spawnSync(process.execPath, [installer, '--install', '--settings', settings, '--install-dir', installDir], {encoding:'utf8'});
    assert.equal(installed.status, 0, installed.stderr);
    const backup = fs.readdirSync(directory).find(name => name.includes('streaming-backup'));
    assert.equal(fs.readFileSync(path.join(directory, backup), 'utf8'), source);
    const updated = fs.readFileSync(settings, 'utf8');
    assert.ok(updated.includes('// preserve comment'));
    assert.ok(updated.includes('"editor.fontSize":14,'));
    const launcher = path.join(installDir, 'claude-stream-wrapper.sh');
    assert.ok(updated.includes(JSON.stringify(launcher)));
    const fake = path.join(directory, 'fake-cli.cjs');
    fs.writeFileSync(fake, 'process.stdin.pipe(process.stderr); console.log(JSON.stringify(process.argv.slice(2))); process.exitCode=7;');
    const args = ['--output-format', 'stream-json', 'literal $HOME `command` "quoted"'];
    const launched = spawnSync(launcher, [process.execPath, fake, ...args], {encoding:'utf8', input:'input preserved', env:{...process.env,PATH:''}});
    assert.equal(launched.status, 7);
    assert.deepEqual(JSON.parse(launched.stdout), [...args, '--include-partial-messages']);
    assert.equal(launched.stderr, 'input preserved');
    const helper = spawnSync(launcher, [process.execPath, fake, '--version'], {encoding:'utf8'});
    assert.deepEqual(JSON.parse(helper.stdout), ['--version']);
    fs.writeFileSync(settings, '{"claudeCode.claudeProcessWrapper":"/existing/wrapper"}');
    const conflict = spawnSync(process.execPath, [installer, '--install', '--settings', settings, '--install-dir', installDir], {encoding:'utf8'});
    assert.equal(conflict.status, 1);
    assert.equal(fs.readFileSync(settings, 'utf8'), '{"claudeCode.claudeProcessWrapper":"/existing/wrapper"}');
  } finally {
    fs.rmSync(directory, {recursive:true,force:true});
  }
});
