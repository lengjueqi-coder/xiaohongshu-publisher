const test = require('node:test');
const assert = require('node:assert/strict');
const { webcrypto } = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, 'static/platforms.js'), 'utf8');
const platforms = ['xhs', 'douyin', 'channels'];
const draft = {
  profile_dir: 'Profile A', images: ['/fixtures/01.jpg', '/fixtures/02.jpg'], title: '印样',
  content: '同一份正文', tags: ['摄影'], publish_mode: 'immediate', schedule_time: '',
  visibility: '仅自己可见', allow_copy: true, mentions: [], activities: []
};

function storage() {
  const values = new Map();
  return {getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, value),
    removeItem: key => values.delete(key)};
}

function page({session = storage(), confirm = () => true, onRequest = () => {}} = {}) {
  const requested = [], captions = [];
  const bindings = {douyin: {account: '抖音 A', account_id:'dy-A'}, channels: {account:'视频号 A', account_id:'wx-A'}};
  const state = {activeProfile:'Profile A', accounts:[{dir:'Profile A', identity:{nickname:'小红书 A', user_id:'xhs-A'}}]};
  const toggles = platforms.map(target => ({dataset:{target}, checked:false}));
  const publish = {dataset:{action:'publish'}};
  const elements = new Map();
  const element = id => {
    if (!elements.has(id)) elements.set(id, {
      style:{}, classList:{add(){},remove(){}}, querySelector: () => ({}),
      querySelectorAll: selector => selector === '[data-target]' ? toggles : selector === '[data-action]' ? [publish] : []
    });
    return elements.get(id);
  };
  const document = {getElementById:element, querySelectorAll:()=>[], body:{dataset:{}}};
  const window = {confirm(caption) { captions.push(caption); return confirm({bindings,state}); },
    distributionUI:{attach(){},showRecords(){}}};
  async function fetch(url, options) {
    const body = options?.body ? JSON.parse(options.body) : null;
    requested.push({url, body});
    const result = await onRequest(url, body);
    if (result) return result;
    if (url === '/api/version') return response({xhs_sync_review:true});
    if (url === '/api/platforms/tabs') return response({bindings, tabs:[]});
    if (url === '/api/distribution/create') return response({batches:[{id:'batch-1',state:'running'}]});
    throw Error('Unexpected test route '+url);
  }
  vm.runInNewContext(source, {document, window, fetch, state, crypto:webcrypto, TextEncoder,
    currentPayload:()=>structuredClone(draft), sessionStorage:session, localStorage:storage()});
  return {ui:window.platformWorkspace, state, toggles, publish, requested, captions, element};
}

function response(body, ok=true) { return {ok, json:async()=>body}; }
const created = p => p.requested.filter(r => r.url === '/api/distribution/create').map(r => r.body);
const fromXhs = (p, prepareOnly=false) => p.ui.fromXhs(['douyin','channels'], structuredClone(draft), false, prepareOnly);

test('confirmation freezes the three displayed account IDs and the reviewed post', async () => {
  const p = page({confirm({bindings,state}) {
    state.activeProfile = 'Profile B';
    state.accounts[0].identity.user_id = 'xhs-B';
    bindings.douyin.account_id = 'dy-B';
    return true;
  }});
  await fromXhs(p);
  assert.equal(p.captions.length, 1);
  for (const id of ['xhs-A','dy-A','wx-A']) assert.ok(p.captions[0].includes(id));
  const [sent] = created(p);
  assert.deepEqual(Object.keys(sent.targets), platforms);
  assert.deepEqual(sent.expected_accounts, {xhs:'xhs-A',douyin:'dy-A',channels:'wx-A'});
  assert.equal(sent.profile_dir, 'Profile A');
  assert.deepEqual(sent.targets.xhs.payload.images, draft.images);
  for (const target of [sent.targets.douyin, sent.targets.channels]) {
    assert.deepEqual(target.files, draft.images);
    assert.equal(target.content, draft.content);
  }
  assert.equal(sent.targets.douyin.visibility, 'private');
  assert.equal(sent.targets.channels.visibility, 'public');
});

test('HTTP failure, refresh, prepare and submit reuse the reviewed three-platform batch', async () => {
  const session = storage();
  const first = page({session, onRequest(url) {
    if (url === '/api/distribution/create') return response({error:'HTTP 503'}, false);
  }});
  await assert.rejects(fromXhs(first, true), /HTTP 503/);
  const [attempt] = created(first);
  const backend = url => {
    if (url === '/api/distribution/create') return response({batches:[{id:'existing-batch', prepare_only:true, state:'paused'}]});
    if (url === '/api/distribution/resume') return response({ok:true});
  };
  const retry = page({session,onRequest:backend});
  await fromXhs(retry, true);
  assert.deepEqual(created(retry), [attempt]);
  assert.ok(!retry.requested.some(r => r.url.endsWith('/resume')));
  const refreshed = page({session,onRequest:backend});
  await fromXhs(refreshed);
  assert.deepEqual(created(refreshed), [{...attempt, prepare_only:false}]);
  assert.ok(attempt.request_id);
  assert.deepEqual(refreshed.requested.filter(r => r.url.endsWith('/resume')).map(r=>r.body),
    [{id:'existing-batch',remaining_only:true}]);
});

test('declined confirmation creates no request for the selected accounts', async () => {
  const p = page({confirm:()=>false});
  await fromXhs(p);
  assert.equal(p.captions.length, 1);
  assert.deepEqual(created(p), []);
});

test('workbench stops if the active profile changes during the three-target account lookup', async () => {
  let release, queries = 0;
  const p = page({onRequest(url) {
    if (url === '/api/platforms/tabs' && ++queries === 2) {
      return new Promise(resolve => {release = () => resolve(response({bindings:{},tabs:[]}));});
    }
  }});
  p.ui.select('douyin');
  await new Promise(resolve => setImmediate(resolve));
  for (const toggle of p.toggles) {toggle.checked = true; toggle.onchange();}
  p.publish.onclick();
  p.state.activeProfile = 'Profile B';
  release();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(p.ui.busy, false);
  assert.deepEqual(p.captions, []);
  assert.deepEqual(created(p), []);
  assert.match(p.element('pagePlatforms').innerHTML, /账号已切换/);
});
