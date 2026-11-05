const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");

function workspace() {
  const elements = new Map();
  const get = (id) => {
    if (!elements.has(id)) elements.set(id, {
      value: "", textContent: "", innerHTML: "", className: "", style: {}, hidden: false,
      classList: { toggle() {} }, querySelectorAll: () => [], setAttribute() {}, removeAttribute() {}, addEventListener() {},
    });
    return elements.get(id);
  };
  const context = vm.createContext({
    document: { getElementById: get, querySelectorAll: () => [], querySelector: () => get("main"), addEventListener() {} },
    setTimeout, clearTimeout, console, ResizeObserver: class { observe() {} disconnect() {} },
  });
  const source = fs.readFileSync(path.join(__dirname, "static/app.js"), "utf8")
    .replace('init().catch((err) => log(err.message));', "");
  vm.runInContext(source + "\nglobalThis.workspace = {state, loadProfile, displayProfile, generateProfile, showView, renderCurrentAccount, currentPayload, generateCopy, invalidateCopies, polishCurrentCopy, selectStyle, firstUsableImage, applyExplicitSize, renderRecommendation, generateCover, saveCover, saveConfig, checkForUpdates, maybeCheckForUpdates, startUpdateChecks, appendImportedPaths, validatePublishImageCount, updateImageCount};", context);
  return { context, get, ...context.workspace };
}

test("immediate form clears schedule and respects original checkbox", () => {
  const app=workspace();
  app.get("publishMode").value="immediate";app.get("schedulePreview").value="立即发布";
  app.get("originalChk").checked=false;
  const payload=app.currentPayload();
  assert.equal(payload.schedule_time,"");assert.equal(payload.original,false);
});

test("random copy control actually selects an allowed style", async () => {
  const app=workspace();app.state.current={name:"素材"};app.get("randomStyleChk").checked=true;
  let requested;
  app.context.fetch=async (_url,options)=>{requested=JSON.parse(options.body);return {ok:true,json:async()=>({copy:{title:"标题",content:"正文",tags:["摄影"],style:requested.style}})};};
  await app.generateCopy("invalid-style");
  assert.ok(["praise","professional","process","opinion","story"].includes(requested.style));
});

test("a collage in first position is the actual preview cover", () => {
  const app=workspace();
  app.state.current={selected_images:["/x/四宫格_1.jpg","/x/original.jpg"]};
  assert.equal(app.firstUsableImage(),"/x/四宫格_1.jpg");
  app.state.excluded.add("/x/四宫格_1.jpg");
  assert.equal(app.firstUsableImage(),"/x/original.jpg");
});

test("images dragged from separate folders append as independent publish paths", () => {
  const app=workspace();
  app.state.current={path:"/folder-a",selected_images:["/folder-a/one.jpg"]};
  app.appendImportedPaths(["/managed/two.jpg","/managed/three.png"]);
  assert.deepEqual(app.state.current.selected_images,["/folder-a/one.jpg","/managed/two.jpg","/managed/three.png"]);
});

test("the publish image maximum is explicit and is never silently truncated", () => {
  const app=workspace();
  assert.doesNotThrow(()=>app.validatePublishImageCount(Array.from({length:18},(_,i)=>String(i))));
  assert.throws(()=>app.validatePublishImageCount(Array.from({length:19},(_,i)=>String(i))),/最多发布 18 张/);
  app.updateImageCount(19);
  assert.match(app.get("imageCountTotal").textContent,/最多发布 18 张/);
});

test("all native image ratios fit wide and narrow panes without distortion", () => {
  const app=workspace();
  for (const [w,h] of [[1600,900],[1600,1200],[1200,1600],[6960,4640]]) {
    for (const paneWidth of [150,240,352,513]) {
      const img={naturalWidth:w,naturalHeight:h,style:{},closest:()=>({clientWidth:paneWidth})};
      app.applyExplicitSize(img);
      assert.ok(parseFloat(img.style.width)<=paneWidth);
      assert.ok(Math.abs(parseFloat(img.style.width)/parseFloat(img.style.height)-w/h)<.015);
    }
  }
});

test("late copy cannot overwrite another material group", async () => {
  const app=workspace();app.state.current={path:"/old"};
  let finish;app.context.fetch=()=>new Promise(r=>finish=r);
  const pending=app.generateCopy();
  app.state.current={path:"/new"};app.invalidateCopies();app.get("titleInput").value="新素材";
  finish({ok:true,json:async()=>({copy:{title:"旧标题",content:"旧正文",tags:[]}})});
  await pending;
  assert.equal(app.get("titleInput").value,"新素材");
  assert.equal(Object.keys(app.state.styleCopies).length,0);
  assert.equal(app.get("generateBtn").disabled,false);
});

test("switching material clears the prior draft even if AI fails", async () => {
  const app=workspace();app.get("titleInput").value="旧人物标题";app.get("contentInput").value="旧人物正文";
  app.context.fetch=async()=>({ok:false,json:async()=>({error:"超时"})});
  app.renderRecommendation({path:"/new",name:"新素材",selected_images:[]});
  await new Promise(r=>setTimeout(r,0));
  assert.equal(app.get("titleInput").value,"");assert.equal(app.get("contentInput").value,"");
});

test("regeneration makes a fresh request and sends the selected account", async () => {
  const app=workspace();app.state.current={path:"/A"};app.state.activeProfile="Profile 1";
  let calls=0;
  app.context.fetch=async (_url,o)=>{calls++;assert.equal(JSON.parse(o.body).profile_dir,"Profile 1");return {ok:true,json:async()=>({copy:{title:`标题${calls}`,content:"正文",tags:[]}})};};
  await app.generateCopy();app.invalidateCopies();await app.generateCopy();
  assert.equal(calls,2);assert.equal(app.get("titleInput").value,"标题2");
});

test("explicit style selection ignores the random checkbox", async () => {
  const app=workspace();app.state.current={path:"/A"};app.get("randomStyleChk").checked=true;
  app.context.fetch=async (_url,o)=>{assert.equal(JSON.parse(o.body).style,"process");return {ok:true,json:async()=>({copy:{title:"标题",content:"正文",tags:[]}})};};
  await app.selectStyle("process");assert.equal(app.state.style,"process");
});

test("polish sends current edits and will not replace edits made while waiting", async () => {
  const app=workspace();app.get("titleInput").value="用户标题";app.get("contentInput").value="用户正文";
  let finish;app.context.fetch=(_url,o)=>{assert.equal(JSON.parse(o.body).content,"用户正文");return new Promise(r=>finish=r);};
  const pending=app.polishCurrentCopy();app.get("contentInput").value="继续手改";
  finish({ok:true,json:async()=>({copy:{title:"AI标题",content:"AI正文",tags:[]}})});await pending;
  assert.equal(app.get("contentInput").value,"继续手改");assert.equal(app.get("polishBtn").disabled,false);
});

test("failed polish preserves current title and body", async () => {
  const app=workspace();app.get("titleInput").value="保留标题";app.get("contentInput").value="保留正文";
  app.context.fetch=async()=>({ok:false,json:async()=>({error:"DeepSeek 调用失败"})});
  await assert.rejects(app.polishCurrentCopy(),/DeepSeek/);
  assert.equal(app.get("titleInput").value,"保留标题");assert.equal(app.get("contentInput").value,"保留正文");
});

test("polish changes only title/body and preserves literal topic input", async () => {
  const app=workspace();app.get("titleInput").value="原题";app.get("contentInput").value="原文";
  const tags="#原话题  #第二个\n#第三个";app.get("tagsInput").value=tags;
  app.context.fetch=async()=>({ok:true,json:async()=>({copy:{title:"润色题",content:"润色文",tags:["不同标签"],engine:"deepseek"}})});
  await app.polishCurrentCopy();
  assert.equal(app.get("titleInput").value,"润色题");assert.equal(app.get("tagsInput").value,tags);
});

test("empty account clears fields and switching restores its own draft", async () => {
  const app = workspace();
  app.get("profileKeywords").value = "旧账号关键词";
  app.context.fetch = async () => ({ ok: true, json: async () => ({ profile_dir: "Default", profile: null }) });
  await app.loadProfile();
  assert.equal(app.get("profileKeywords").value, "");
  app.state.profileDrafts.set("Default", { manual_keywords: "摄影", manual_topics: "写真" });
  app.state.activeProfile = "Profile 1";
  app.displayProfile();
  assert.equal(app.get("profileKeywords").value, "");
  app.state.activeProfile = "Default";
  app.displayProfile();
  assert.equal(app.get("profileKeywords").value, "摄影");
});

test("late profile load cannot overwrite a newly selected account", async () => {
  const app = workspace();
  let finish;
  app.context.fetch = () => new Promise((resolve) => { finish = resolve; });
  const pending = app.loadProfile();
  app.state.activeProfile = "Profile 1";
  app.get("profileKeywords").value = "新账号";
  finish({ ok: true, json: async () => ({ profile_dir: "Default", profile: { manual_keywords: "旧账号" } }) });
  await pending;
  assert.equal(app.get("profileKeywords").value, "新账号");
});

test("generation result stays with its origin after an account switch", async () => {
  const app = workspace();
  app.get("profileKeywords").value = "摄影";
  let finish;
  app.context.fetch = () => new Promise((resolve) => { finish = resolve; });
  const pending = app.generateProfile();
  app.state.activeProfile = "Profile 1";
  app.displayProfile();
  const before = app.get("profileBox").textContent;
  finish({ ok: true, json: async () => ({ profile_dir: "Default", profile: { positioning: "摄影工作室" } }) });
  await pending;
  assert.equal(app.state.profiles.get("Default").positioning, "摄影工作室");
  assert.equal(app.state.profiles.has("Profile 1"), false);
  assert.equal(app.get("profileBox").textContent, before);
});

test("failed generation preserves saved profile and unsaved inputs", async () => {
  const app = workspace();
  app.state.profiles.set("Default", { positioning: "已保存定位", topics: [] });
  app.get("profileKeywords").value = "新关键词";
  app.context.fetch = async () => ({ ok: false, json: async () => ({ error: "AI 请求失败" }) });
  await assert.rejects(app.generateProfile(), /AI 请求失败/);
  assert.equal(app.get("profileKeywords").value, "新关键词");
  assert.match(app.get("profileBox").innerHTML, /已保存定位/);
  assert.equal(app.get("genProfileBtn").disabled, false);
});

test("profile navigation hides publish actions and returns without losing fields", () => {
  const app = workspace();
  app.get("profileKeywords").value = "摄影";
  app.showView("profile");
  assert.equal(app.get("pageProfile").style.display, "");
  assert.equal(app.get("pagePublish").style.display, "none");
  assert.equal(app.get("bottomBar").hidden, true);
  app.showView("publish");
  assert.equal(app.get("bottomBar").hidden, false);
  assert.equal(app.get("profileKeywords").value, "摄影");
});

const identitySource = fs.readFileSync(path.join(__dirname, "static/account-identity.js"), "utf8");
async function extract(state, selectors = {}) {
  return vm.runInNewContext(identitySource, {
    location: { hostname: "www.xiaohongshu.com", pathname: "/user/profile/visited-author" },
    window: { __INITIAL_STATE__: state }, document: { querySelector: (s) => selectors[s] || null },
  });
}

test("identity extraction uses signed-in state rather than visited author", async () => {
  const data = await extract({ user: { userInfo: { userId: "operator", nickname: "摄影师唐唐", imageb: "https://xhscdn.com/avatar" } }, userPageData: { nickname: "访问的作者" } });
  assert.equal(data.user_id, "operator");
  assert.equal(data.nickname, "摄影师唐唐");
});

test("ambiguous profile content is never reported as authenticated identity", async () => {
  const data = await extract({ userPageData: { userId: "visited-author", nickname: "访问的作者" } });
  assert.equal(data.logged_in, null);
});

test("explicit logout is reported without cached author details", async () => {
  const data = await extract({}, { '.login-container, form[action*="login"], .login-box': {} });
  assert.equal(data.logged_in, false);
});


test("cover result stays with original group when user switches while waiting", async () => {
  const app=workspace();
  app.state.current={path:"/original",selected_images:["/original/photo.jpg"]};
  app.state.activeProfile="Profile 1";
  app.get("coverPrompt").value="保留人物，逆光";
  app.get("coverModel").value="gemini-3.1-flash-image-preview";
  app.get("coverResolution").value="4k";
  app.get("coverPlatform").value="apiyi";
  app.get("coverSaveChk").checked=false;
  let resolve;
  app.context.fetch=()=>new Promise(r=>{resolve=r;});
  const request=app.generateCover();
  app.state.current={path:"/different",selected_images:["/different/photo.jpg"]};
  resolve({ok:true,json:async()=>({ok:true,b64:"picture",mime:"image/png",width:3000,height:4000,elapsed_sec:20})});
  await request;
  assert.equal(app.state.coverDraft,undefined);
  assert.equal(app.state.coverResult.target.group,"/original");
  assert.equal(app.get("coverSaveBtn").hidden,false);
  assert.match(app.get("runLog").textContent,/原素材文件夹/);
});

test("saving a previous cover never inserts it into a different account's current group", async () => {
  const app=workspace();
  app.state.current={path:"/different",selected_images:["/different/photo.jpg"]};
  app.state.activeProfile="Profile 2";
  let sent;
  app.context.fetch=async (url, options)=>{sent=JSON.parse(options.body);return {ok:true,json:async()=>({ok:true,path:"/original/AI封面/cover.png"})};};
  await app.saveCover({b64:"picture",mime:"image/png"},{group:"/original",profile:"Profile 1"});
  assert.equal(sent.folder,"/original/AI封面");
  assert.match(sent.filename,/.png$/);
  assert.equal(app.state.current.selected_images.length,1);
});


test("new GitHub release appears in the global notice with a clear update action", async () => {
  const app=workspace();
  app.context.fetch=async()=>({ok:true,json:async()=>({ok:true,update_available:true,latest_version:"2026.11.7",release:{version:"2026.11.7",package_ready:true,notes:"修复AI封面"}})});
  await app.checkForUpdates(true);
  assert.equal(app.get("updateBanner").hidden,false);
  assert.equal(app.get("updateButtonLabel").textContent,"有新版本");
  assert.equal(app.get("installUpdateBtn").disabled,false);
  assert.match(app.get("installUpdateBtn").textContent,/一键更新/);
  assert.match(app.get("updateTitle").textContent,/2026.11.7/);
});

test("latest-version and network-failure states are visible even for automatic checks", async () => {
  const app=workspace();
  app.context.fetch=async()=>({ok:true,json:async()=>({ok:true,update_available:false,current_version:"2026.11.7"})});
  await app.checkForUpdates(true);
  assert.match(app.get("updateStatus").textContent,/2026.11.7.*已是最新/);
  app.context.fetch=async()=>({ok:true,json:async()=>({ok:true,update_available:false,current_version:"2026.11.8",latest_version:"2026.11.7"})});
  await app.checkForUpdates(true);
  assert.match(app.get("updateStatus").textContent,/本地 v2026\.11\.8.*GitHub 最新 v2026\.11\.7/);
  app.context.fetch=async()=>{throw Error("offline");};
  await app.checkForUpdates(true);
  assert.match(app.get("updateStatus").textContent,/检查失败/);
  assert.match(app.get("updateButtonLabel").textContent,/重试/);
  assert.equal(app.get("checkUpdatesBtn").disabled,false);
});

test("concurrent version checks share one request", async () => {
  const app=workspace();let resolve;let calls=0;
  app.context.fetch=()=>{calls++;return new Promise(r=>{resolve=r;});};
  const first=app.checkForUpdates(true);const second=app.checkForUpdates(false);
  assert.equal(calls,1);
  resolve({ok:true,json:async()=>({ok:true,update_available:false,current_version:"2026.11.7"})});
  await Promise.all([first,second]);
  assert.equal(app.state.updateCheckPending,null);
});

test("focus checks are throttled and hidden tabs skip polling", async () => {
  const app=workspace();let calls=0;
  vm.runInContext('Date.now = () => 500000;',app.context);
  app.context.fetch=async()=>{calls++;return {ok:true,json:async()=>({ok:true,update_available:false,current_version:"2026.11.7"})};};
  app.state.lastUpdateCheckAt=400000;
  app.maybeCheckForUpdates();assert.equal(calls,0);
  app.state.lastUpdateCheckAt=300000;app.context.document.visibilityState="hidden";
  app.maybeCheckForUpdates();assert.equal(calls,0);
  app.context.document.visibilityState="visible";
  app.maybeCheckForUpdates();await app.state.updateCheckPending;
  assert.equal(calls,1);
});

test("update checks start before other workspace requests and register one ten-minute timer", async () => {
  const app=workspace();let interval;const events=[];
  app.context.setInterval=(callback,ms)=>{interval={callback,ms};return 1;};
  app.context.window={addEventListener:(name)=>events.push(name)};
  app.context.document.addEventListener=(name)=>events.push(name);
  app.context.fetch=async()=>({ok:true,json:async()=>({ok:true,update_available:false,current_version:"2026.11.7"})});
  app.startUpdateChecks();await app.state.updateCheckPending;app.startUpdateChecks();
  assert.equal(interval.ms,600000);
  assert.deepEqual(events,["visibilitychange","focus"]);
});
