const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const worker = fs.readFileSync(path.join(__dirname, 'vendor/xiaohongshu-skills/extension/background.js'), 'utf8');

function harness() {
  const personalDraft = {id: 1, url: 'https://creator.xiaohongshu.com/publish/publish', active: true};
  const tabs = new Map([[1, personalDraft]]);
  const saved = {};
  const captures = [];
  const event = {addListener() {}, removeListener() {}};
  const chrome = {
    tabs: {
      onCreated: event, onUpdated: event,
      async get(id) {
        if (!tabs.has(id)) throw Error('closed tab');
        return tabs.get(id);
      },
      async create(options) {
        assert.equal(options.active, false);
        const tab = {...options, id: tabs.size + 1, status: 'complete'};
        tabs.set(tab.id, tab);
        return tab;
      }
    },
    alarms: {create() {}, onAlarm: event},
    storage: {local: {get: () => new Promise(() => {})}, session: {
      async get(key) { return {[key]: saved[key]}; },
      async set(values) { Object.assign(saved, values); }
    }},
    debugger: {
      onDetach: event,
      async attach() {}, async detach() {},
      async sendCommand(target, method) {
        captures.push([target.tabId, method]);
        return {data: 'image'};
      }
    }
  };
  const ctx = vm.createContext({chrome, URL, setTimeout, clearTimeout});
  vm.runInContext(worker, ctx);
  return {ctx, tabs, personalDraft, captures};
}

test('publish and management use dedicated inactive tabs while a personal draft is active', async () => {
  const {ctx, tabs, personalDraft, captures} = harness();
  const publish = await ctx.handleCommand({method:'xhs_owned_tab', params:{scope:'publish', create:true}});
  const manager = await ctx.handleCommand({method:'xhs_owned_tab', params:{scope:'management', create:true}});
  assert.notEqual(publish.id, manager.id);
  assert.notEqual(publish.id, personalDraft.id);
  assert.equal(tabs.get(1), personalDraft);
  await ctx.cmdScreenshot({scope:'management', tabId:manager.id});
  assert.deepEqual(captures, [[manager.id, 'Page.captureScreenshot']]);
  await assert.rejects(ctx.xhsTarget({scope:'publish', tabId:personalDraft.id}), /已变化/);
  tabs.delete(publish.id);
  await assert.rejects(ctx.xhsTarget({scope:'publish', tabId:publish.id}), /已关闭/);
  assert.equal(tabs.size, 2);
});
