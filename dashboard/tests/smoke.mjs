// Optional real-browser smoke: temporary credentials/ledger, synthetic declaration fixture and a local minimal-probe worker; no SSH or shared hosts.
import {spawn} from 'node:child_process';
import {mkdir, mkdtemp, readFile, rm, writeFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {dirname, join} from 'node:path';
import assert from 'node:assert/strict';
import {fileURLToPath} from 'node:url';

const coreRoot = fileURLToPath(new URL('../..', import.meta.url));
const temp = await mkdtemp(join(tmpdir(), 'dashboard-review-browser-'));
const siteRoot = join(temp, 'site');
const stateRoot = join(temp, 'state');

async function writeSyntheticSite(root) {
 const put = async (rel, content) => {
  const full = join(root, rel);
  await mkdir(dirname(full), {recursive: true});
  await writeFile(full, content);
 };
 const j = obj => JSON.stringify(obj, null, 2);

 await put('infra/inventory.yml', j({
  all: {
   vars: {
    ansible_user: 'operator',
    project_root: '/srv/agentiac',
    backend_port: 18080,
    ondemand_backend_port: 18090,
    idle_unload_minutes: 30,
    nodejs_binary: '/usr/bin/node',
    rclone_config_dir: '/srv/agentiac/rclone',
    backup_destination: 'remote:backups',
    gateway_port: 14000,
    chatgpt_port: 14001,
    resource_prefix: 'agentiac',
   },
   children: {
    workers: {
     hosts: {
      workera: {
       ansible_host: 'workera.invalid',
       node_class: 'orin',
       ondemand_persistent: true,
       default_profile: 'small-q6',
       gateway_alias: 'workera-worker',
       tunnel_port: 18101,
       backend_port: 18080,
       ondemand_backend_port: 18090,
       strata_backend: 'strata-0.2.12-qfn-gsq-rco-iq3_xxs',
       strata_trial_roots: {'0.2.12': '/srv/agentiac/strata-0.2.12'},
      },
      workerc: {
       ansible_host: 'workerc.invalid',
       node_class: 'rtx5090',
       borrowed: true,
       backend: 'strata',
       strata_api_port: 18085,
       strata_tunnel_port: 18115,
       ondemand_backend_port: 18090,
       strata_default_backend: 'strata-0.2.11-qfn-ud-iq3_xxs',
       strata_trial_roots: {
        '0.2.8': '/srv/agentiac/strata-0.2.8',
        '0.2.11': '/srv/agentiac/strata-0.2.11',
       },
      },
      workerd: {
       ansible_host: 'workerd.invalid',
       node_class: 'rtx5090',
       borrowed: true,
       backend: 'strata',
       strata_api_port: 18085,
       strata_tunnel_port: 18114,
       ondemand_backend_port: 18090,
       strata_default_backend: 'strata-0.2.8-qfn-gyro-m-tq2_0',
       strata_trial_roots: {'0.2.8': '/mnt/data/agentiac-strata-0.2.8'},
      },
      control: {
       ansible_connection: 'local',
       ansible_host: 'localhost',
       node_class: 'laptop',
      },
     },
    },
   },
  },
 }));
 await put('profiles/small-q6.json', j({nodes: ['orin'], model: 'q6'}));

 const manifest = (id, version, modelRepo, quantPath, workers, context = 262144) => j({
  id,
  repository: 'https://example.invalid/Strata.git',
  source_version: version,
  source_revision: '1'.repeat(40),
  source_ref: 'v' + version,
  ggml_revision: '3cf03257f219' + '0'.repeat(30),
  cuda_toolkit_major: 13,
  family: 'qfn',
  model: 'Qwen/Qwen3.8-Flash-Next',
  model_repository: modelRepo,
  model_revision: '2'.repeat(40),
  context,
  contexts: [context],
  served_model: 'qwen3.8-flash-next',
  files: [{path: quantPath, size: 70000000000, sha256: 'a'.repeat(64)}],
  workers: Object.fromEntries(workers.map(w => [w, {cuda_arch: '87'}])),
 });

 await put('backends/strata-0.2.12-qfn-gsq-rco-iq3_xxs.json',
  manifest('strata-0.2.12-qfn-gsq-rco-iq3_xxs', '0.2.12', 'synth/Qwen3.8-Flash-Next-GSQ-RCO-GGUF', 'iq3_xxs.gguf', ['workera']));
 await put('backends/strata-0.2.11-qfn-ud-iq3_xxs.json',
  manifest('strata-0.2.11-qfn-ud-iq3_xxs', '0.2.11', 'synth/Qwen3.8-Flash-Next-GGUF', 'ud-iq3_xxs.gguf', ['workerc']));
 await put('backends/strata-0.2.8-qfn-gyro-m-tq2_0.json',
  manifest('strata-0.2.8-qfn-gyro-m-tq2_0', '0.2.8', 'synth/Qwen3.8-Flash-Next-Gyro-GGUF', 'tq2_0.gguf', ['workerc', 'workerd'], 131072));
 await put('backends/strata-0.1.45-qfn-iq3_xxs.json',
  manifest('strata-0.1.45-qfn-iq3_xxs', '0.1.45', 'synth/Qwen3.8-Flash-Next-GGUF', 'iq3_xxs.gguf', ['workerc'], 131072));

 await put('docs/lifecycle.md', '# Lifecycle\nRetired 0.1.45 trial.\n');
 await put('backends/lifecycle.yml', j({
  version: 1,
  backends: {
   'strata-0.1.45-qfn-iq3_xxs': {
    catalog_status: 'archived',
    reason: 'retired trial',
    evidence: 'docs/lifecycle.md',
   },
  },
 }));

 await put('agents/pi/package.json', j({
  dependencies: {'@mariozechner/pi-coding-agent': '1.5.0'},
 }));
 await put('agents/pi/package-lock.json', j({
  packages: {
   '': {dependencies: {'@mariozechner/pi-coding-agent': '^1.5.0'}},
   'node_modules/@mariozechner/pi-coding-agent': {version: '1.5.0'},
  },
 }));
 await put('agents/agents.yml', j([
  {id: 'pi', owner: 'project', hosts: ['control'], endpoint: '/v1', lock: 'agents/pi/package-lock.json'},
 ]));
 await put('infra/components.yml', j([
  {
   id: 'pi', kind: 'agent', owner: 'project', hosts: ['control'], policy: 'notify',
   source: {type: 'npm', package: '@mariozechner/pi-coding-agent'},
   pin: {version: '1.5.0', lock: 'agents/pi/package-lock.json'},
  },
  {
   id: 'strata-workera', kind: 'engine', owner: 'project', hosts: ['workera'], policy: 'manual',
   gate: 'staged', source: {type: 'github', repository: 'synth/Strata'},
   pin: {version: '0.2.12', revision: '1'.repeat(40), lock: 'backends/strata-0.2.12-qfn-gsq-rco-iq3_xxs.json'},
  },
 ]));
 await put('infra/retention.yml', j({
  workera: {
   serving: [{backend: 'strata-0.2.12-qfn-gsq-rco-iq3_xxs', path: '/srv/agentiac/strata-0.2.12', class: 'persistent', form: 'full', reason: 'serving'}],
   rollback: [], candidate: [], quarantine: [], retired: [], staging: [], areas: [],
  },
  workerc: {
   serving: [{backend: 'strata-0.2.11-qfn-ud-iq3_xxs', path: '/srv/agentiac/strata-0.2.11', class: 'persistent', form: 'full', reason: 'serving'}],
   rollback: [{backend: 'strata-0.2.8-qfn-gyro-m-tq2_0', path: '/srv/agentiac/strata-0.2.8', class: 'persistent', form: 'full', reason: 'N-1'}],
   candidate: [], quarantine: [], retired: [], staging: [], areas: [],
  },
  workerd: {
   serving: [{backend: 'strata-0.2.8-qfn-gyro-m-tq2_0', path: '/mnt/data/agentiac-strata-0.2.8', class: 'persistent', form: 'full', reason: 'serving'}],
   rollback: [], candidate: [], quarantine: [], retired: [], staging: [], areas: [],
  },
  control: {serving: [], rollback: [], candidate: [], quarantine: [], retired: [], staging: [], areas: []},
 }));
 await put('gateway/config.yaml', j({
  model_list: [
   {model_name: 'workera-worker', litellm_params: {model: 'openai/iq3', api_base: 'http://127.0.0.1:18101/v1'}},
   {model_name: 'workerc-worker-ud', litellm_params: {model: 'openai/ud', api_base: 'http://127.0.0.1:18115/v1'}},
   {model_name: 'workerc-worker-gyro', litellm_params: {model: 'openai/gyro', api_base: 'http://127.0.0.1:18115/v1'}},
   {model_name: 'workerd-worker-gyro', litellm_params: {model: 'openai/gyro', api_base: 'http://127.0.0.1:18114/v1'}},
  ],
 }));
 await put('gateway/local-coder.json', j({
  version: 2,
  alias: 'local-coder',
  default: 'workera-strata-0.2.12-qfn-gsq-rco-iq3_xxs-256k',
  targets: {
   'workera-strata-0.2.12-qfn-gsq-rco-iq3_xxs-256k': {
    source_alias: 'workera-worker', node: 'workera', backend: 'strata-0.2.12-qfn-gsq-rco-iq3_xxs',
    context_window: 262144, max_tokens: 16384,
   },
   'workerc-strata-0.2.11-qfn-ud-iq3_xxs-256k': {
    source_alias: 'workerc-worker-ud', node: 'workerc', backend: 'strata-0.2.11-qfn-ud-iq3_xxs',
    context_window: 262144, max_tokens: 16384,
   },
   'workerc-strata-0.2.8-qfn-gyro-m-tq2_0-128k': {
    source_alias: 'workerc-worker-gyro', node: 'workerc', backend: 'strata-0.2.8-qfn-gyro-m-tq2_0',
    context_window: 131072, max_tokens: 16384,
   },
   'workerd-strata-0.2.8-qfn-gyro-m-tq2_0-128k': {
    source_alias: 'workerd-worker-gyro', node: 'workerd', backend: 'strata-0.2.8-qfn-gyro-m-tq2_0',
    context_window: 131072, max_tokens: 16384,
   },
  },
  legacy_targets: {},
  legacy_sources: {},
 }));
 await put('agents/pi/models.json', j({providers: {'local-infra': {models: [{id: 'local-coder'}]}}}));
 await put('agents/pi/router.json', j({low: 'local-coder', high: 'local-coder'}));
}

const children = [];
let socket;
const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
try {
 await writeSyntheticSite(siteRoot);
 await mkdir(stateRoot, {recursive: true});
 const python = spawn(process.env.DASHBOARD_PYTHON || join(coreRoot, '.venv-dashboard/bin/python'), ['-u', '-c', `
import socket, pathlib, uvicorn
from agentiac.management import app as api
from agentiac.management.session import SessionStore
api.SESSION_PATH = pathlib.Path('${temp}/session.sqlite')
SessionStore(api.SESSION_PATH).initialize('browser-fixture-password')
from agentiac.operations import ledger
ledger.DB_PATH = pathlib.Path('${temp}/operations.sqlite')
ledger.LOG_DIR = pathlib.Path('${temp}/jobs')
from agentiac.management import reads
reads.hardware.latest = lambda *a: None
# The worker targets a real catalogue ID but transports only to a disposable local child.
import threading
from agentiac.operations import probe, worker
probe.endpoint_for = lambda root, target: {'connection': 'local'}
ready = threading.Event()
def work():
    store = ledger.Ledger()
    try:
        with worker.WorkerLock(store):
            store.publish_worker('browser-fixture', ['workera'])
            ready.set()
            while True:
                store.publish_worker('browser-fixture', ['workera'])
                worker.run_once(store, probe.LiveProbeExecutor(api.ROOT), 'browser-fixture',
                                targets=['workera'], locked=True, publish=True)
                threading.Event().wait(0.1)
    finally:
        store.close()
threading.Thread(target=work, daemon=True).start()
assert ready.wait(3), 'fixture worker did not publish readiness'
sock = socket.socket(); sock.bind(('127.0.0.1', 0)); sock.listen(128)
print(sock.getsockname()[1], flush=True)
uvicorn.Server(uvicorn.Config(api.app, log_level='error', proxy_headers=False)).run(sockets=[sock])
`], {cwd: coreRoot, env: {...process.env, AGENTIAC_ROOT: siteRoot, AGENTIAC_STATE_ROOT: stateRoot, PYTHONPATH: join(coreRoot, 'src')}, stdio: ['ignore', 'pipe', 'pipe']});
 children.push(python);
 let output = '';
 python.stdout.on('data', b => output += b);
 python.stderr.on('data', b => process.stderr.write(b));
 for (let i = 0; i < 100 && !output.includes('\n'); i++) await wait(50);
 const port = Number(output.trim());
 assert(port > 0);
 // Chrome may create DevToolsActivePort before writing it, and on CI it has occasionally published no
 // port at all. Capture its exit status and stderr so a failure is diagnosable, and relaunch once with a
 // fresh profile; the first failure is still printed, so a recurring startup problem stays visible.
 const launchChrome = async attempt => {
  const profile = `${temp}/chrome-${attempt}`;
  const chrome = spawn(process.env.CHROME_BIN || '/usr/bin/google-chrome', ['--headless', '--no-sandbox', '--disable-dev-shm-usage', '--no-first-run', '--no-default-browser-check', '--remote-debugging-port=0', '--remote-debugging-address=127.0.0.1', '--user-data-dir=' + profile, 'about:blank'], {stdio: ['ignore', 'ignore', 'pipe']});
  children.push(chrome);
  let stderr = '', exited = null;
  chrome.stderr.on('data', b => {stderr = (stderr + b).slice(-20000);});
  chrome.on('exit', (code, signal) => {exited = {code, signal};});
  const started = Date.now();
  while (!exited && Date.now() - started < 15000) {
   try {const p = Number((await readFile(profile + '/DevToolsActivePort', 'utf8')).split('\n')[0]); if (p > 0) return p;} catch {}
   await wait(50);
  }
  console.error(`Chrome launch ${attempt}: no DevTools port after ${Date.now() - started} ms; exit ${JSON.stringify(exited)}; stderr tail:\n${stderr.slice(-4000)}`);
  chrome.kill('SIGKILL');
  return 0;
 };
 let debugPort = await launchChrome(1);
 if (!(debugPort > 0)) debugPort = await launchChrome(2);
 assert(debugPort > 0, 'Chrome did not publish a DevTools port in two launches');
 const targets = await (await fetch(`http://127.0.0.1:${debugPort}/json/list`)).json();
 socket = new WebSocket(targets.find(t => t.type === 'page').webSocketDebuggerUrl);
 await new Promise((resolve, reject) => {socket.onopen = resolve; socket.onerror = reject;});
 const pending = new Map();
 let seq = 0;
 socket.onmessage = event => {const msg = JSON.parse(event.data); if (msg.id) {const p = pending.get(msg.id); if (p) {pending.delete(msg.id); msg.error ? p.reject(new Error(JSON.stringify(msg.error))) : p.resolve(msg.result);}}};
 const call = (method, params = {}) => new Promise((resolve, reject) => {const id = ++seq; pending.set(id, {resolve, reject}); socket.send(JSON.stringify({id, method, params}));});
 const evaluate = async expression => {const r = await call('Runtime.evaluate', {expression, returnByValue: true, awaitPromise: true}); if (r.exceptionDetails) throw new Error(JSON.stringify(r.exceptionDetails)); return r.result.value;};
 const until = async expression => {for (let i = 0; i < 100; i++) {if (await evaluate(expression)) return; await wait(100);} throw new Error('timeout: ' + expression);};
 // Structural accessibility checks for whatever is on screen: names, ids, table headers, toggles, headings.
 const a11yProblems = `(()=>{const out=[];
  if(!document.documentElement.lang) out.push('html lang missing');
  const name=el=>{const label=el.getAttribute('aria-label');if(label&&label.trim())return label;
   const by=el.getAttribute('aria-labelledby');if(by)return by.split(' ').map(i=>document.getElementById(i)?.textContent||'').join(' ');
   if(['INPUT','SELECT','TEXTAREA'].includes(el.tagName)){const wrap=el.closest('label');if(wrap&&wrap.textContent.trim())return wrap.textContent;
    if(el.id){const target=document.querySelector('label[for="'+CSS.escape(el.id)+'"]');if(target)return target.textContent;}return el.getAttribute('title')||'';}
   return (el.textContent||'').trim()||el.getAttribute('title')||'';};
  for(const el of document.querySelectorAll('button,a[href],input,select,textarea'))if(!name(el).trim())out.push(el.tagName+' without accessible name: '+el.outerHTML.slice(0,80));
  const ids={};for(const el of document.querySelectorAll('[id]'))ids[el.id]=(ids[el.id]||0)+1;
  for(const [id,count] of Object.entries(ids))if(count>1)out.push('duplicate id '+id);
  for(const table of document.querySelectorAll('table'))if(!table.querySelector('thead th'))out.push('table without header cells');
  for(const button of document.querySelectorAll('button.toggle'))if(!button.hasAttribute('aria-expanded'))out.push('toggle without aria-expanded');
  for(const heading of document.querySelectorAll('h1,h2,h3,h4'))if(!heading.textContent.trim())out.push('empty heading');
  return out;})()`;
 const a11y = async where => assert.deepEqual(await evaluate(a11yProblems), [], 'accessibility: ' + where);
 await call('Page.navigate', {url: `http://127.0.0.1:${port}/dashboard/`});
 await until(`!!document.querySelector('input[name=password]')`);
 await a11y('login');
 await evaluate(`document.querySelector('input[name=password]').value='wrong';document.querySelector('form').requestSubmit()`);
 await until(`document.querySelector('[role=alert]')?.textContent.includes('credential')`);
 await evaluate(`document.querySelector('input[name=password]').value='browser-fixture-password';document.querySelector('form').requestSubmit()`);
 await until(`!!document.querySelector('table')`);
 const snapshot = await evaluate(`document.querySelector('.content .status').textContent`);
 assert.match(snapshot, /declarations read: .*nodes \d+s ago/, 'overview did not show the list snapshot age');
 await evaluate(`[...document.querySelectorAll('button')].find(b=>b.textContent==='refresh').click()`);
 await until(`!!document.querySelector('table') && document.querySelector('.content .status').textContent !== ${JSON.stringify(snapshot)}`);
 await evaluate(`[...document.querySelectorAll('button')].find(b=>b.textContent.includes('theme:')).click()`);
 assert.equal(await evaluate(`document.documentElement.dataset.theme`), 'dark');
 // Every view, with one row of each table expanded so details and Related are checked too.
 for (const view of ['Overview', 'Nodes', 'Models', 'Engines', 'Agents', 'Deployments', 'Jobs', 'Monitoring', 'Configuration']) {
  await evaluate(`[...document.querySelectorAll('nav button')].find(b=>b.textContent===${JSON.stringify(view)}).click()`);
  await until(`document.querySelector('nav button.active')?.textContent===${JSON.stringify(view)} && !!document.querySelector('.content .panel')`);
  await evaluate(`for(const table of document.querySelectorAll('.panel table')){table.querySelector('button.toggle')?.click()}`);
  await wait(300);
  await a11y(view);
 }
 await evaluate(`[...document.querySelectorAll('nav button')].find(b=>b.textContent==='Nodes').click()`);
 await until(`!!document.querySelector('tbody tr')`);
 assert.equal(await evaluate(`document.querySelector('tbody tr td:last-child').textContent`), 'unknown');
 await evaluate(`[...document.querySelectorAll('nav button')].find(b=>b.textContent==='Jobs').click()`);
 await until(`!!document.querySelector('input[name=target]')`);
 await evaluate(`document.querySelector('input[name=target]').value='workera';document.querySelector('input[name=key]').value='browser-probe';document.querySelector('input[name=target]').form.requestSubmit()`);
 await until(`document.querySelector('.detail')?.textContent.includes('status: succeeded')`);
 assert.equal(await evaluate(`document.querySelector('.detail').textContent.includes('minimal-os-cpu-memory')`), true);
 await evaluate(`[...document.querySelectorAll('nav button')].find(b=>b.textContent==='Nodes').click()`);
 await until(`document.querySelector('tbody')?.textContent.includes('linux-proc-v1')`);
 // Deep link to a node: its row opens with relations, including the probe job just run.
 await evaluate(`location.hash='#/nodes/workera'`);
 await until(`[...document.querySelectorAll('.related a')].some(a=>a.textContent.startsWith('jobs/')) && [...document.querySelectorAll('.related a')].some(a=>a.textContent.startsWith('models/'))`);
 await evaluate(`[...document.querySelectorAll('.related a')].find(a=>a.textContent.startsWith('models/')).click()`);
 await until(`location.hash.startsWith('#/models/') && !document.getElementById('row-nodes-workera') && [...document.querySelectorAll('tr.detail-row .related a')].some(a=>a.textContent==='nodes/workera')`);
 await evaluate(`history.back()`);
 // Wait for the node view itself: right after the hash changes the models view (no job links) can still be shown.
 await until(`location.hash==='#/nodes/workera' && !!document.getElementById('row-nodes-workera') && [...document.querySelectorAll('tr.detail-row .related a')].some(a=>a.textContent.startsWith('jobs/'))`);
 await evaluate(`[...document.querySelectorAll('.related a')].find(a=>a.textContent.startsWith('jobs/')).click()`);
 await until(`location.hash.startsWith('#/jobs/') && document.querySelector('.detail')?.textContent.includes('status: succeeded')`);
 // Engines: its own table (no "pending" banner); a current engine's details link its backend manifests.
 await evaluate(`[...document.querySelectorAll('nav button')].find(b=>b.textContent==='Engines').click()`);
 await until(`location.hash==='#/engines' && !!document.querySelector('tr[id^="row-engines-"]') && !document.querySelector('[role=status]')?.textContent.includes('pending')`);
 // Historical releases are hidden by default, accessible through the scope and direct links.
 assert.equal(await evaluate(`!!document.getElementById('row-engines-strata-0.1.45')`), false);
 await evaluate(`[...document.querySelectorAll('button')].find(b=>b.textContent==='Show history').click()`);
 await until(`!!document.getElementById('row-engines-strata-0.1.45')`);
 await evaluate(`location.hash='#/engines/strata-0.1.45'`);
 await until(`!!document.getElementById('engine-details-strata-0.1.45') && document.querySelector('.engines-panel').textContent.includes('hidden by the current filters')`);
 await evaluate(`[...document.querySelectorAll('button')].find(b=>b.textContent==='Show in list').click()`);
 await until(`document.getElementById('engine-details-strata-0.1.45') && !document.querySelector('.engines-panel').textContent.includes('hidden by the current filters')`);
 await evaluate(`location.hash='#/engines?node=workerc&use=default'`);
 await until(`!!document.querySelector('.engine-filters')`);
 await wait(200);
 assert.equal(await evaluate(`!!document.getElementById('row-engines-strata-0.2.8')`), false, 'workerd default must not match workerc rollback');
 assert.equal(await evaluate(`!!document.getElementById('row-engines-strata-0.2.11')`), true, 'workerc default must be present');
 await evaluate(`history.back()`);
 await until(`location.hash.startsWith('#/engines/strata-0.1.45') && !!document.getElementById('engine-details-strata-0.1.45')`);
 await evaluate(`location.hash='#/engines'`);
 await until(`!!document.getElementById('row-engines-strata-0.2.12')`);
 await evaluate(`(()=>{const input=document.querySelector('[aria-label="Search engines"]');Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(input,'0.2.11');input.dispatchEvent(new Event('input',{bubbles:true}));})()`);
 await until(`location.hash.includes('q=0.2.11') && !!document.getElementById('row-engines-strata-0.2.11') && !document.getElementById('row-engines-strata-0.2.12')`);
 await evaluate(`[...document.querySelectorAll('button')].find(b=>b.textContent==='Reset filters').click()`);
 await until(`location.hash==='#/engines' && !!document.getElementById('row-engines-strata-0.2.12')`);
 // Drive the filters with real keyboard events and inspect Chrome's accessibility tree.
 await evaluate(`document.querySelector('.engine-filters select').focus()`);
 for (const expected of ['Node', 'Use', 'Search engines']) {
  await call('Input.dispatchKeyEvent', {type: 'keyDown', key: 'Tab', code: 'Tab', windowsVirtualKeyCode: 9});
  await call('Input.dispatchKeyEvent', {type: 'keyUp', key: 'Tab', code: 'Tab', windowsVirtualKeyCode: 9});
  const name = await evaluate(`document.activeElement.getAttribute('aria-label') || document.activeElement.closest('label')?.textContent.trim().split('All nodes')[0].split('All uses')[0].trim()`);
  assert.equal(name, expected, 'keyboard focus order');
 }
 await call('Input.insertText', {text: '0.2.11'});
 await until(`location.hash.includes('q=0.2.11') && !!document.getElementById('row-engines-strata-0.2.11')`);
 assert.equal(await evaluate(`document.activeElement.getAttribute('aria-label')`), 'Search engines', 'search must keep keyboard focus');
 await call('Input.dispatchKeyEvent', {type: 'keyDown', key: 'Tab', code: 'Tab', windowsVirtualKeyCode: 9});
 await call('Input.dispatchKeyEvent', {type: 'keyUp', key: 'Tab', code: 'Tab', windowsVirtualKeyCode: 9});
 assert.equal(await evaluate(`document.activeElement.textContent`), 'Reset filters');
 await call('Input.dispatchKeyEvent', {type: 'keyDown', key: 'Enter', code: 'Enter', windowsVirtualKeyCode: 13, text: '\r', unmodifiedText: '\r'});
 await call('Input.dispatchKeyEvent', {type: 'keyUp', key: 'Enter', code: 'Enter', windowsVirtualKeyCode: 13});
 await until(`location.hash==='#/engines' && !!document.getElementById('row-engines-strata-0.2.12')`);
 const ax = (await call('Accessibility.getFullAXTree')).nodes.filter(n => !n.ignored);
 for (const [role, name] of [['combobox', 'Scope'], ['combobox', 'Node'], ['combobox', 'Use'], ['textbox', 'Search engines']])
  assert(ax.some(n => n.role?.value === role && n.name?.value === name), `missing accessibility tree ${role} ${name}`);
 assert(ax.some(n => n.role?.value === 'button' && n.name?.value?.startsWith('Show details for strata-')), 'missing named engine details control');
 // Check the computed foreground/background ratio of visible text in both themes.
 const contrast = `(()=>{
  const rgb=s=>{const m=s.match(/[0-9.]+/g);return m&&m.length>=3?m.slice(0,3).map(Number):null};
  const luminance=c=>c.map(x=>{x/=255;return x<=.04045?x/12.92:((x+.055)/1.055)**2.4}).reduce((a,x,i)=>a+x*[.2126,.7152,.0722][i],0);
  const out=[];
  for(const e of document.querySelectorAll('.engines-panel *, nav button.active')){
   if(![...e.childNodes].some(n=>n.nodeType===3&&n.textContent.trim()) || !e.getClientRects().length || e.closest('.sr-only') || e.disabled)continue;
   const style=getComputedStyle(e);let parent=e,bg;
   while(parent){const c=getComputedStyle(parent).backgroundColor;if(c!=='rgba(0, 0, 0, 0)'&&c!=='transparent'){bg=rgb(c);break}parent=parent.parentElement}
   const fg=rgb(style.color);if(!fg||!bg)continue;
   const a=luminance(fg),b=luminance(bg),ratio=(Math.max(a,b)+.05)/(Math.min(a,b)+.05);
   const size=parseFloat(style.fontSize),bold=parseInt(style.fontWeight,10)>=700,minimum=size>=24||(bold&&size>=18.66)?3:4.5;
   if(ratio+0.01<minimum)out.push({text:e.textContent.trim().slice(0,55),ratio:Math.round(ratio*100)/100,minimum});
  }return out})()`;
 for (const theme of ['dark', 'light']) {
  if (await evaluate(`document.documentElement.dataset.theme`) !== theme) await evaluate(`[...document.querySelectorAll('button')].find(b=>b.textContent.includes('theme:')).click()`);
  assert.deepEqual(await evaluate(contrast), [], `${theme} contrast below threshold`);
 }
 await evaluate(`(()=>{const b=document.createElement('button');b.id='contrast-negative-control';b.textContent='contrast negative control';b.style.color='#777';b.style.backgroundColor='#777';document.querySelector('.engines-panel').append(b)})()`);
 assert((await evaluate(contrast)).some(item => item.text === 'contrast negative control'), 'contrast checker failed to catch its negative control');
 await evaluate(`document.getElementById('contrast-negative-control').remove()`);
 await evaluate(`[...document.querySelectorAll('button')].find(b=>b.textContent.includes('theme:')).click()`); // return to dark
 for (const width of [320, 760, 1440]) {
  await call('Emulation.setDeviceMetricsOverride', {width, height: 1000, deviceScaleFactor: 1, mobile: false});
  await wait(150);
  const overflow = await evaluate(`({width:innerWidth,scroll:document.documentElement.scrollWidth,items:[...document.querySelectorAll('body *')].filter(e=>e.scrollWidth>e.clientWidth+2 && getComputedStyle(e).overflowX==='visible').slice(0,15).map(e=>e.tagName+'.'+e.className+' '+e.clientWidth+'/'+e.scrollWidth)})`);
  assert(overflow.scroll <= overflow.width, `engines page overflows at ${width}px: ${JSON.stringify(overflow)}`);
 }
 await call('Emulation.clearDeviceMetricsOverride');
 // An older API keeps all releases visible while lifecycle filters are unavailable.
 await evaluate(`window.e4Fetch=window.fetch; window.fetch=async (...args)=>{const response=await window.e4Fetch(...args);if(!String(args[0]).startsWith('/api/v1/engines?'))return response;const body=await response.json();for(const row of body.engines)delete row.lifecycle;return new Response(JSON.stringify(body),{status:response.status,headers:{'Content-Type':'application/json'}})}`);
 await evaluate(`[...document.querySelectorAll('button')].find(b=>b.textContent==='refresh').click()`);
 await until(`document.querySelector('.engines-panel')?.textContent.includes('Lifecycle information unavailable') && !!document.getElementById('row-engines-strata-0.1.45')`);
 assert.equal(await evaluate(`[...document.querySelectorAll('.engine-filters select')].every(e=>e.disabled)`), true, 'old API filters must be disabled');
 await evaluate(`window.fetch=window.e4Fetch`);
 await evaluate(`[...document.querySelectorAll('button')].find(b=>b.textContent==='refresh').click()`);
 await until(`!document.querySelector('.engines-panel')?.textContent.includes('Lifecycle information unavailable') && !document.getElementById('row-engines-strata-0.1.45')`);
 // Two changing pagination generations must fail visibly and retain the complete previous data.
 await evaluate(`window.fetch=async (...args)=>{const url=String(args[0]);if(!url.startsWith('/api/v1/engines?'))return window.e4Fetch(...args);if(url.includes('cursor=')){const b=structuredClone(window.e4Page);b.engines=b.engines.slice(1);b.next_cursor=null;b.snapshot.generated_at='1999-01-01T00:00:00Z';return new Response(JSON.stringify(b),{headers:{'Content-Type':'application/json'}})}const r=await window.e4Fetch(...args);const b=await r.json();window.e4Page=structuredClone(b);b.engines=b.engines.slice(0,1);b.next_cursor='fixture';return new Response(JSON.stringify(b),{headers:{'Content-Type':'application/json'}})}`);
 await evaluate(`[...document.querySelectorAll('button')].find(b=>b.textContent==='refresh').click()`);
 await until(`document.querySelector('[role=alert]')?.textContent.includes('Engine declarations changed while loading')`);
 assert.equal(await evaluate(`!!document.getElementById('row-engines-strata-0.2.12') && !!document.getElementById('row-engines-strata-0.2.11')`), true, 'partial pages replaced the last complete list');
 await evaluate(`window.fetch=window.e4Fetch`);
 await evaluate(`[...document.querySelectorAll('button')].find(b=>b.textContent==='refresh').click()`);
 await until(`!document.querySelector('[role=alert]') && !!document.getElementById('row-engines-strata-0.2.12')`);
 await evaluate(`location.hash='#/engines'`);
 await until(`!!document.getElementById('row-engines-strata-0.2.12')`);
 await evaluate(`[...document.querySelectorAll('tr[id^="row-engines-"]')].find(tr=>tr.textContent.includes('local-coder selected')).querySelector('td.id a').click()`);
 await until(`location.hash.startsWith('#/engines/') && [...document.querySelectorAll('tr.detail-row .related a')].some(a=>a.textContent.startsWith('backend/')) && [...document.querySelectorAll('tr.detail-row .related a')].some(a=>a.textContent.startsWith('models/'))`);
 // Models: the first column is the node-first name; route keys only appear as the secondary line.
 await evaluate(`[...document.querySelectorAll('nav button')].find(b=>b.textContent==='Models').click()`);
 await until(`!!document.querySelector('tr[id^="row-models-"] td.id a')`);
 assert.deepEqual(await evaluate(`[...document.querySelectorAll('tr[id^="row-models-"] td.id a')].map(a=>a.textContent).filter(t=>/^strata-/.test(t))`), [], 'models view still leads with engine-first names');
 // Deployments: control deploy, route and dashboard history (empty in a fresh checkout), no "pending" banner.
 await evaluate(`[...document.querySelectorAll('nav button')].find(b=>b.textContent==='Deployments').click()`);
 await until(`location.hash==='#/deployments' && [...document.querySelectorAll('h4')].some(h=>h.textContent==='Control (ops deploy)') && !!document.querySelector('.panel a[href^="#/models/"]') && !document.querySelector('[role=status]')?.textContent.includes('pending')`);
 await evaluate(`location.hash='#/configuration/component%2Fpi'`);
 await until(`[...document.querySelectorAll('tr.detail-row .related a')].some(a=>a.textContent==='agents/pi')`);
 // Opt-in review screenshots of an open detail with relations (DASHBOARD_SMOKE_SCREENSHOTS=<absolute dir>).
 if (process.env.DASHBOARD_SMOKE_SCREENSHOTS) {
  await evaluate(`location.hash='#/nodes/workera'`);
  await until(`!!document.querySelector('tr.detail-row .related a')`);
  for (const width of [1440, 760]) {
   await call('Emulation.setDeviceMetricsOverride', {width, height: 1100, deviceScaleFactor: 1, mobile: false});
   await evaluate(`document.getElementById('row-nodes-workera').scrollIntoView({block:'start'})`);
   await wait(300);
   const shot = await call('Page.captureScreenshot', {format: 'png'});
   await writeFile(join(process.env.DASHBOARD_SMOKE_SCREENSHOTS, `related-${width}.png`), Buffer.from(shot.data, 'base64'));
  }
  await evaluate(`location.hash='#/engines'`);
  await until(`!!document.querySelector('tr[id^="row-engines-"]')`);
  await evaluate(`[...document.querySelectorAll('tr[id^="row-engines-"]')].find(tr=>tr.textContent.includes('local-coder selected')).querySelector('td.id a').click()`);
  await until(`!!document.querySelector('tr.detail-row .related a')`);
  await call('Emulation.setDeviceMetricsOverride', {width: 1440, height: 1100, deviceScaleFactor: 1, mobile: false});
  await evaluate(`window.scrollTo(0,0)`);
  await wait(300);
  await writeFile(join(process.env.DASHBOARD_SMOKE_SCREENSHOTS, 'engines-1440.png'), Buffer.from((await call('Page.captureScreenshot', {format: 'png'})).data, 'base64'));
  for (const width of [320, 760]) {
   await call('Emulation.setDeviceMetricsOverride', {width, height: 1100, deviceScaleFactor: 1, mobile: false});
   await evaluate(`window.scrollTo(0,0)`);
   await wait(200);
   await writeFile(join(process.env.DASHBOARD_SMOKE_SCREENSHOTS, `engines-${width}.png`), Buffer.from((await call('Page.captureScreenshot', {format: 'png'})).data, 'base64'));
  }
  await evaluate(`location.hash='#/models'`);
  await until(`!!document.querySelector('tr[id^="row-models-"]')`);
  await evaluate(`window.scrollTo(0,0)`);
  await wait(300);
  await writeFile(join(process.env.DASHBOARD_SMOKE_SCREENSHOTS, 'models-1440.png'), Buffer.from((await call('Page.captureScreenshot', {format: 'png'})).data, 'base64'));
  await evaluate(`location.hash='#/configuration'`);
  await until(`[...document.querySelectorAll('h4')].some(h=>h.textContent==='Route targets')`);
  await evaluate(`[...document.querySelectorAll('h4')].find(h=>h.textContent==='Route').scrollIntoView({block:'start'})`);
  await wait(300);
  await writeFile(join(process.env.DASHBOARD_SMOKE_SCREENSHOTS, 'route-1440.png'), Buffer.from((await call('Page.captureScreenshot', {format: 'png'})).data, 'base64'));
  await evaluate(`location.hash='#/deployments'`);
  await until(`[...document.querySelectorAll('h4')].some(h=>h.textContent==='Control (ops deploy)')`);
  await evaluate(`window.scrollTo(0,0)`);
  await wait(300);
  await writeFile(join(process.env.DASHBOARD_SMOKE_SCREENSHOTS, 'deployments-1440.png'), Buffer.from((await call('Page.captureScreenshot', {format: 'png'})).data, 'base64'));
  await call('Emulation.clearDeviceMetricsOverride');
  await evaluate(`location.hash='#/configuration/component%2Fpi'`);
  await until(`[...document.querySelectorAll('tr.detail-row .related a')].some(a=>a.textContent==='agents/pi')`);
 }

 await call('Page.reload');
 await until(`!!document.querySelector('table')`);
 await until(`location.hash==='#/configuration/component%2Fpi' && [...document.querySelectorAll('tr.detail-row .related a')].some(a=>a.textContent==='agents/pi')`);
 await evaluate(`[...document.querySelectorAll('button')].find(b=>b.textContent==='logout').click()`);
 await until(`!!document.querySelector('input[name=password]')`);
 console.log('PASS: login errors, authenticated read, refresh, dark theme, accessibility of every view, unknown hardware, probe acceptance/completion/snapshot, relation deep links/back/reload, engines, deployments, node-first names, session restore, logout');
} finally {
 socket?.close();
 for (const child of children.reverse()) {child.kill('SIGTERM');}
 await wait(500);
 await rm(temp, {recursive: true, force: true, maxRetries: 5, retryDelay: 200});
}
