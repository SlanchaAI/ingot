// Execute every inline script from the shipped page; no network or external packages.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const inspector = require('node:inspector');
const {promisify} = require('node:util');

(async () => {
  const session = new inspector.Session();
  session.connect();
  const post = promisify(session.post.bind(session));
  await post('Profiler.enable');
  await post('Profiler.startPreciseCoverage', {callCount: true, detailed: true});
  const elements = new Map();
  function element() {
    return {textContent: '', hidden: true, value: '', dataset: {}, style: {setProperty() {}},
      classList: {add() {}, remove() {}, toggle() {}},
      querySelectorAll: () => [], querySelector: () => element(),
      addEventListener() {}, focus() {}, options: [],
      get innerHTML() { return this._html ?? String(this.textContent).replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;'); },
      set innerHTML(value) { this._html = value; }};
  }
  const get = id => {
    if (!elements.has(id)) elements.set(id, element());
    return elements.get(id);
  };
  const document = {hidden: true, documentElement: element(), body: element(),
    querySelector: get, getElementById: id => get('#' + id), querySelectorAll: () => [],
    createElement: element, addEventListener() {}};
  const context = vm.createContext({document, console, matchMedia: () => ({matches: true}),
    setInterval() {}, setTimeout() {}, requestAnimationFrame() {},
    performance: {now: () => 0}, window: {},
    fetch: async url => ({ok: true, json: async () => ({authenticated: true,
      email: 'reviewer@example.test', role: 'approver'}), text: async () =>
        JSON.stringify({langfuse_url: 'https://traces.example.test'})})});
  const scripts = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
  scripts.forEach((code, i) => vm.runInContext(code, context,
    {filename: `ui/static/index.html#script-${i + 1}`}));
  const run = expression => vm.runInContext(expression, context);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(get('#who-email').textContent, 'reviewer@example.test (approver)');
  assert.equal(get('#who').hidden, false);
  assert.equal(get('#lf').href, 'https://traces.example.test');
  assert.equal(run('esc("<script>\\\"&")'), '&lt;script&gt;&quot;&amp;');
  assert.match(run('statusChip({pending:true})'), /awaiting review/);
  assert.match(run('statusChip({status:"running"})'), /running/);
  assert.match(run('statusChip({has_tasks:false})'), /no evals/);
  assert.match(run('statusChip({has_tasks:true})'), /evals/);
  assert.match(run('historyKpis(null)'), /history unavailable/);
  assert.match(run('historyKpis({revisions:{pdf:[{},{}]},audit:{records:[],total:5}})'), /newest 0 listed/);
  assert.match(run('delta(10,5,true)'), /win.*-5/);
  assert.match(run('delta(10,5,false)'), /lose.*-5/);
  assert.equal(run('delta(null,5,true)'), '');
  assert.match(run('buildCompare({model:"<bad>",ab:{champion:{scores:[0.2]},challenger:{scores:[0.9]}}})'), /&lt;bad&gt;/);
  context.fetch = async () => ({ok: false, status: 409, text: async () => '{"detail":"stale evidence"}'});
  await assert.rejects(run('j("/api/promote/pdf")'), /stale evidence/);
  context.fetch = async () => ({ok: false, status: 502, text: async () => 'gateway unavailable'});
  await assert.rejects(run('j("/api/history")'), /gateway unavailable/);
  context.fetch = async () => ({ok: true, text: async () => '{"result":"saved"}'});
  assert.equal((await run('j("/api/config")')).result, 'saved');
  await run('act("#pending-msg", async () => {throw new Error("refused")})');
  assert.equal(get('#pending-msg').textContent, '⛔ refused');
  run('clearLoadError("#pending-msg")');
  assert.equal(get('#pending-msg').textContent, '⛔ refused');
  run('say("#skills-msg", "failed poll", true, "load"); clearLoadError("#skills-msg")');
  assert.equal(get('#skills-msg').textContent, '');
  context.fetch = async () => {throw new Error('offline');};
  document.hidden = false;
  await run('refresh()');
  assert.match(get('#skills-msg').textContent, /could not load skills/);
  assert.match(get('#history-msg').textContent, /could not load history/);
  assert.equal(run('refreshing'), false);
  const {result} = await post('Profiler.takePreciseCoverage');
  const coverage = result.filter(item => item.url.startsWith('ui/static/index.html#script-'));
  assert.equal(coverage.length, scripts.length);
  fs.writeFileSync(process.argv[3], JSON.stringify(coverage, null, 2));
  await post('Profiler.stopPreciseCoverage');
  session.disconnect();
  console.log(`Browser: ${scripts.length} inline scripts executed; rendering, API success/failure, action and poll recovery passed`);
})().catch(error => {console.error(error); process.exitCode = 1;});
