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
      listeners: {},
      classList: { toggle() {} }, querySelectorAll: () => [], setAttribute() {}, removeAttribute() {},
      addEventListener(name, listener) { (this.listeners[name] ||= []).push(listener); },
    });
    return elements.get(id);
  };
  const context = vm.createContext({
    document: { getElementById: get, querySelectorAll: () => [], querySelector: () => get("main"), addEventListener() {} },
    setTimeout, clearTimeout, console, confirm: () => true, window: { addEventListener() {} },
    ResizeObserver: class { observe() {} disconnect() {} },
  });
  const source = fs.readFileSync(path.join(__dirname, "static/app.js"), "utf8")
    .replace('init().catch((err) => log(err.message));', "");
  vm.runInContext(source + "\nglobalThis.workspace = {state, loadProfile, displayProfile, generateProfile, showView, renderCurrentAccount, currentPayload, generateCopy, invalidateCopies, polishCurrentCopy, selectStyle, firstUsableImage, applyExplicitSize, renderRecommendation, generateCover, saveCover, savePublishPreferences, saveConfig, loadConfig, loadQueueItem, changeGroup, updateQueuePreviewControls, leaveQueuePreview, checkForUpdates, maybeCheckForUpdates, startUpdateChecks, appendImportedPaths, validatePublishImageCount, updateImageCount, refreshTopicTemplateOptions, renderPublishingResources, applyTopicTemplateSelection, parseLocalTemplateTopics, saveLocalTopicTemplate, syncPublishingResources, testTextModel, formatCopyEngine, renderProfile, setManualPending, fillPublish, publishNow, enqueueCurrent, confirmEnqueue, confirmPublished, previewSchedule, finishCurrentDraft, useProfileHotKeyword, bindEvents};", context);
  return { context, get, ...context.workspace };
}

test("topic templates require an explicit selection", () => {
  const app=workspace();
  app.refreshTopicTemplateOptions(["摄影模板"]);
  assert.match(app.get("topicTemplate").innerHTML, /^<option value="">不使用模板<\/option>/);
  assert.equal(app.get("topicTemplate").value, "");
  app.get("topicTemplate").value="摄影模板";
  app.refreshTopicTemplateOptions(["摄影模板"]);
  assert.equal(app.get("topicTemplate").value, "摄影模板");
});

test("manual topic template names stay visibly unverified until platform sync", () => {
  const app=workspace();
  app.state.publishingResources={};
  app.refreshTopicTemplateOptions(["手填模板"]);
  assert.match(app.get("topicTemplate").innerHTML,/\[未验证名称\] 手填模板/);
  app.state.publishingResources={templates:[{name:"手填模板",source:"platform"}]};
  app.refreshTopicTemplateOptions(["手填模板"]);
  assert.match(app.get("topicTemplate").innerHTML,/\[平台已同步\] 手填模板/);
});

test("local topic templates freeze their exact topics in the publish payload", () => {
  const app=workspace();
  app.state.localTopicTemplates=[{name:"艺考常用",tags:["艺考照","演员资料照"],source:"local"}];
  app.refreshTopicTemplateOptions(["平台模板"]);
  app.get("topicTemplate").value="local:艺考常用";
  app.applyTopicTemplateSelection();
  const payload=app.currentPayload();
  assert.equal(payload.local_topic_template,"艺考常用");
  assert.equal(payload.topic_template,"");
  assert.deepEqual(Array.from(payload.tags),["艺考照","演员资料照"]);
  assert.equal(app.get("localTemplateTopics").value,"#艺考照 #演员资料照");
});

test("local template paste accepts separators, deduplicates, and enforces ten topics", () => {
  const app=workspace();
  assert.deepEqual(Array.from(app.parseLocalTemplateTopics("#艺考照，演员资料照\n#艺考照")),["艺考照","演员资料照"]);
  assert.throws(()=>app.parseLocalTemplateTopics(Array.from({length:11},(_,i)=>`话题${i}`).join(" ")),/1–10/);
});

test("saving a local template sends account-scoped structured template data", async () => {
  const app=workspace();app.state.activeProfile="Profile 2";
  app.get("localTemplateName").value="演员资料";
  app.get("localTemplateTopics").value="#演员资料照 #见组照";
  let sent;
  app.context.fetch=async (url,options)=>{sent={url,body:JSON.parse(options.body)};return {ok:true,json:async()=>({ok:true})};};
  await app.saveLocalTopicTemplate();
  assert.equal(sent.url,"/api/config");
  assert.equal(sent.body.profile_dir,"Profile 2");
  assert.deepEqual(sent.body.local_topic_templates,[{name:"演员资料",tags:["演员资料照","见组照"],source:"local"}]);
  assert.match(app.get("localTemplateStatus").textContent,/已为当前账号保存/);
});

test("account config restores resources, PPToken, publish preferences, and manual pending", async () => {
  const app=workspace();
  app.context.fetch=async()=>({ok:true,json:async()=>({ok:true,config:{
    active_profile:"Profile 2",root_path:"/素材",default_album:"演员合集",default_location:"北京",
    schedule_windows:["19:00-21:00"],default_schedule_window:"19:00-21:00",topic_templates:["手填平台模板"],
    local_topic_templates:[{name:"本地常用",tags:["艺考照"],source:"local"}],
    publishing_resources:{checked_at:"2026-11-06T10:00:00",synced_at:"2026-11-06T10:00:00",templates:[{name:"平台同步模板"}],albums:["合集A"],errors:[]},
    daily_limit:3,visibility:"仅自己可见",publish_mode:"immediate",original_enabled:false,copy_engine:"auto",
    text_provider:"pptoken",text_base_url:"https://example.com/v1",pptoken_model:"gpt-test",has_pptoken_key:true,
    deepseek_model:"deepseek-flash",manual_pending:{history_id:77,title:"待确认标题",created_at:"2026-11-06T10:01:00"},
  }})});
  await app.loadConfig();
  assert.equal(app.state.activeProfile,"Profile 2");
  assert.equal(app.get("visibility").value,"仅自己可见");
  assert.equal(app.get("publishMode").value,"immediate");
  assert.equal(app.get("scheduleWindow").value,"19:00-21:00");
  assert.equal(app.get("originalChk").checked,false);
  assert.equal(app.get("textProvider").value,"pptoken");
  assert.equal(app.get("pptokenSettings").hidden,false);
  assert.equal(app.get("deepseekSettings").hidden,true);
  assert.match(app.get("topicTemplate").innerHTML,/本地常用/);
  assert.match(app.get("topicTemplate").innerHTML,/平台同步模板/);
  assert.match(app.get("accountAlbumOptions").innerHTML,/合集A/);
  assert.equal(app.get("confirmManualPublishBtn").hidden,false);
  assert.equal(app.get("prefillPublishBtn").disabled,true);
  assert.equal(app.state.manualPublishes.get("Profile 2").id,77);
});

test("PPToken results display the real configured platform and model", () => {
  const app=workspace();app.get("textProvider").value="pptoken";app.get("pptokenModel").value="gpt-custom";
  assert.equal(app.formatCopyEngine({ai_used:true,provider:"pptoken",provider_label:"PPToken",model:"gpt-custom",engine:"PPToken · gpt-custom"}),"PPToken · gpt-custom");
  assert.equal(app.formatCopyEngine({ai_used:false,engine:"local-template"}),"本地模板");
});

test("platform resource sync keeps partial results and reports exact failures", async () => {
  const app=workspace();app.state.activeProfile="Profile 1";
  app.context.fetch=async (url,options)=>{
    assert.equal(url,"/api/publishing/resources/sync");
    assert.equal(JSON.parse(options.body).profile_dir,"Profile 1");
    return {ok:true,json:async()=>({ok:true,profile_dir:"Profile 1",resources:{checked_at:"2026-11-06T11:00:00",templates:[{name:"平台模板"}],albums:["合集一"],status:{templates:{ok:true,topics_complete:true},albums:{ok:true}},errors:["未读到第二页合集"]}})};
  };
  await app.syncPublishingResources();
  assert.match(app.get("topicTemplate").innerHTML,/平台模板/);
  assert.match(app.get("accountAlbumOptions").innerHTML,/合集一/);
  assert.match(app.get("resourcesStatus").textContent,/部分读取失败.*未读到第二页合集/);
  assert.equal(app.get("syncResourcesBtn").disabled,false);
});

test("resource status distinguishes stale cached lists from this check", () => {
  const app=workspace();
  app.state.publishingResources={
    checked_at:"2026-11-06T12:00:00",templates_synced_at:"2026-11-05T09:00:00",albums_synced_at:"2026-11-06T11:00:00",
    templates:[{name:"旧平台模板"}],albums:["合集一"],errors:["模板弹窗读取失败"],
    status:{templates:{ok:false,message:"模板弹窗读取失败"},albums:{ok:true}},
  };
  app.renderPublishingResources();
  assert.match(app.get("resourcesStatus").textContent,/最近检查 2026-11-06T12:00:00/);
  assert.match(app.get("resourcesStatus").textContent,/模板缓存，上次成功 2026-11-05T09:00:00/);
  assert.match(app.get("resourcesStatus").textContent,/合集缓存，上次成功 2026-11-06T11:00:00/);
  assert.match(app.get("resourcesStatus").textContent,/部分读取失败/);
});

test("a schedule window removed from settings falls back to the first available window", async () => {
  const app=workspace();
  app.context.fetch=async()=>({ok:true,json:async()=>({ok:true,config:{
    active_profile:"Profile 1",schedule_windows:["18:00-19:00","20:00-21:00"],default_schedule_window:"19:00-20:00",
    topic_templates:[],local_topic_templates:[],publishing_resources:{},daily_limit:3,visibility:"公开可见",publish_mode:"scheduled",
  }})});
  await app.loadConfig();
  assert.equal(app.get("scheduleWindow").value,"18:00-19:00");
});

test("publish preferences persist the chosen schedule window and original default per account", async () => {
  const app=workspace();app.state.activeProfile="Profile 1";
  let sent;
  app.context.fetch=async(url,options)=>{sent={url,body:JSON.parse(options.body)};return {ok:true,json:async()=>({ok:true})};};
  await app.savePublishPreferences({default_schedule_window:"19:00-21:00",original_enabled:false});
  assert.equal(sent.url,"/api/publish/preferences");
  assert.equal(sent.body.profile_dir,"Profile 1");
  assert.equal(sent.body.default_schedule_window,"19:00-21:00");
  assert.equal(sent.body.original_enabled,false);
  assert.equal(app.state.config.original_enabled,false);
});

test("model connection test saves PPToken fields before calling the test endpoint", async () => {
  const app=workspace();app.state.activeProfile="Profile 1";
  app.get("textProvider").value="pptoken";app.get("textBaseUrl").value="https://example.com/v1";
  app.get("pptokenModel").value="gpt-custom";app.get("pptokenKey").value="secret";
  const calls=[];
  app.context.fetch=async (url,options)=>{
    calls.push({url,body:JSON.parse(options.body)});
    return {ok:true,json:async()=>({ok:true})};
  };
  await app.testTextModel();
  assert.equal(calls[0].url,"/api/config");
  assert.equal(calls[0].body.text_provider,"pptoken");
  assert.equal(calls[0].body.pptoken_model,"gpt-custom");
  assert.equal(calls[0].body.pptoken_api_key,"secret");
  assert.equal(calls[1].url,"/api/model/test");
  assert.match(app.get("modelStatus").textContent,/连接成功：PPToken · gpt-custom/);
  assert.equal(app.get("pptokenKey").value,"");
});

test("profile view shows read, empty-content, empty-topic, and source-error counts", () => {
  const app=workspace();
  app.renderProfile({positioning:"摄影工作室",source_mode:"recent",note_count:3,
    source_completeness:{requested:20,read:3,detail_success:3,detail_failed:0,content_success:2,content_empty:1,topic_success:1,topic_empty:2,with_content:2,with_topics:1},source_error:"详情读取部分失败",topics:[]});
  assert.match(app.get("profileBox").innerHTML,/实际读取 3 条/);
  assert.match(app.get("profileBox").innerHTML,/正文成功 2 条、空内容 1 条/);
  assert.match(app.get("profileBox").innerHTML,/话题成功 1 条、空话题 2 条/);
  assert.match(app.get("profileBox").innerHTML,/平台读取失败.*详情读取部分失败/);
});

test("profile generation saves model settings and displays the API model identity", async () => {
  const app=workspace();
  app.get("profileSourceMode").value="manual";
  app.get("profileKeywords").value="摄影";
  app.get("textProvider").value="pptoken";
  app.get("textBaseUrl").value="https://api.example.test/v1";
  app.get("pptokenModel").value="dropdown-model";
  app.get("pptokenKey").value="secret";
  const calls=[];
  app.context.fetch=async (url,options)=>{
    const body=options?.body ? JSON.parse(options.body) : {};
    calls.push({url,body});
    if(url==="/api/config") return {ok:true,json:async()=>({ok:true})};
    if(url==="/api/profile/save") return {ok:true,json:async()=>({ok:true,profile:{manual_keywords:"摄影"},prompts:{}})};
    return {ok:true,json:async()=>({ok:true,profile_dir:"Default",profile:{positioning:"摄影工作室",source_mode:"manual",note_count:0,ai_provider:"pptoken",ai_provider_label:"PPToken",ai_model:"api-model",source_completeness:{requested:0,read:0,with_content:0,with_topics:0},topics:[]}})};
  };
  await app.generateProfile();
  assert.equal(calls[0].url,"/api/config");
  assert.equal(calls[0].body.text_provider,"pptoken");
  assert.equal(calls[0].body.text_base_url,"https://api.example.test/v1");
  assert.equal(calls[0].body.pptoken_model,"dropdown-model");
  assert.equal(calls[0].body.pptoken_api_key,"secret");
  assert.equal(calls[1].url,"/api/profile/save");
  assert.equal(calls[2].url,"/api/profile");
  assert.equal(calls[2].body.source_mode,"manual");
  assert.match(app.get("profileBox").innerHTML,/画像模型：PPToken · api-model/);
  assert.match(app.get("runLog").textContent,/账号画像已由 PPToken · api-model/);
  assert.doesNotMatch(app.get("runLog").textContent,/dropdown-model/);
});

test("manual pending blocks every publish entry until verified or discarded", async () => {
  const app=workspace();let calls=0;app.context.fetch=async()=>{calls++;throw Error("should not fetch");};
  app.setManualPending("Default",{history_id:9,title:"上一条"});
  await assert.rejects(app.fillPublish(),/待确认的发布页面/);
  await assert.rejects(app.publishNow(),/待确认的发布页面/);
  await assert.rejects(app.enqueueCurrent(),/待确认的发布页面/);
  await assert.rejects(app.confirmEnqueue(),/待确认的发布页面/);
  assert.equal(calls,0);
  assert.equal(app.get("immediatePublishBtn").disabled,true);
  assert.equal(app.get("schedulePublishBtn").disabled,true);
});

test("queue snapshot is read-only and switching groups restores saved account preferences", async () => {
  const app=workspace();
  app.state.activeProfile="Profile 1";
  app.state.config={default_album:"默认合集",default_location:"默认地点",visibility:"公开可见",publish_mode:"scheduled",schedule_windows:["19:00-21:00"],topic_templates:[],daily_limit:3};
  Object.assign(app.get("rootPath"),{value:"/素材"});
  app.get("defaultAlbum").value="默认合集";app.get("defaultLocation").value="默认地点";
  app.get("visibility").value="公开可见";app.get("publishMode").value="scheduled";
  app.get("scheduleWindow").value="19:00-21:00";app.get("schedulePreview").value="2026-11-07 20:00";
  app.get("originalChk").checked=true;app.get("mentionsInput").value="";
  const title=app.get("titleInput");
  title.disabled=false;
  app.get("pagePublish").querySelectorAll=()=>[title];
  app.state.queue=[{id:11,status:"submitted",schedule_time:"",payload:{
    folder_path:"/素材/一组",folder_name:"一组",images:[],title:"队列标题",content:"队列正文",
    album:"一次性合集",location:"一次性地点",visibility:"仅自己可见",publish_mode:"immediate",tags:[],
  }}];
  const saved=[];
  app.context.fetch=async(url,options)=>{
    if(url==="/api/config"){saved.push(JSON.parse(options.body));return {ok:true,json:async()=>({ok:true})};}
    if(url==="/api/scan")return {ok:true,json:async()=>({items:[]})};
    throw Error(`unexpected ${url}`);
  };
  app.loadQueueItem(0);
  assert.equal(title.disabled,true);
  assert.equal(app.get("defaultAlbum").value,"一次性合集");
  await app.changeGroup();
  assert.equal(saved.length,1);
  assert.equal(saved[0].default_album,"默认合集");
  assert.equal(saved[0].default_location,"默认地点");
  assert.equal(saved[0].visibility,"公开可见");
  assert.equal(app.get("defaultAlbum").value,"默认合集");
  assert.equal(app.get("visibility").value,"公开可见");
  assert.equal(title.disabled,false);
  assert.equal(app.state.queuePreview,null);
});

test("visibility changes during queue preview do not persist account preferences", async () => {
  const app=workspace();let requests=0;
  app.state.queuePreview={payload:{visibility:"仅自己可见",publish_mode:"immediate"}};
  app.get("visibility").value="公开可见";
  app.get("publishMode").value="scheduled";
  app.context.fetch=async()=>{requests++;return {ok:true,json:async()=>({ok:true})};};
  const result=await app.savePublishPreferences({visibility:"公开可见"});
  assert.equal(result.skipped,true);
  assert.equal(requests,0);
  assert.equal(app.get("visibility").value,"仅自己可见");
  assert.equal(app.get("publishMode").value,"immediate");
});

test("batch plan controls are read-only during queue preview", async () => {
  const app=workspace();
  app.state.queuePreview={payload:{visibility:"仅自己可见",publish_mode:"immediate"},preferences:{planTotal:"15",dailyLimit:"3"}};
  app.updateQueuePreviewControls();
  app.get("planTotal").value="99";
  app.get("dailyLimit").value="1";
  let requests=0;
  app.context.fetch=async()=>{requests++;return {ok:true,json:async()=>({ok:true})};};
  app.bindEvents();
  const planChange=app.get("planTotal").listeners.change?.[0];
  const limitChange=app.get("dailyLimit").listeners.change?.[0];
  assert.equal(app.get("planTotal").disabled,true);
  assert.equal(app.get("dailyLimit").disabled,true);
  await planChange();
  await limitChange();
  assert.equal(requests,0);
  app.leaveQueuePreview();
  assert.equal(app.get("planTotal").value,"15");
  assert.equal(app.get("dailyLimit").value,"3");
});

test("all publish-page data controls are read-only during queue preview", () => {
  const app=workspace();
  const title={id:"titleInput",disabled:false};
  const cover={id:"coverPrompt",disabled:false};
  const coverGenerate={id:"coverGenerateBtn",disabled:false};
  const change={id:"changeGroupBtn",disabled:false};
  app.get("pagePublish").querySelectorAll=()=>[title,cover,coverGenerate,change];
  app.state.queuePreview={payload:{},preferences:{}};
  app.updateQueuePreviewControls();
  assert.equal(title.disabled,true);
  assert.equal(cover.disabled,true);
  assert.equal(coverGenerate.disabled,true);
  assert.equal(change.disabled,false);
  app.leaveQueuePreview();
  assert.equal(title.disabled,false);
  assert.equal(cover.disabled,false);
  assert.equal(coverGenerate.disabled,false);
});

test("switching queue snapshots keeps the original account preferences", () => {
  const app=workspace();
  app.state.config={default_album:"账号合集",default_location:"账号地点",visibility:"公开可见",publish_mode:"scheduled",default_schedule_window:"19:00-21:00",schedule_windows:["19:00-21:00"],original_enabled:false};
  app.get("defaultAlbum").value="账号合集";
  app.get("defaultLocation").value="账号地点";
  app.get("visibility").value="公开可见";
  app.get("publishMode").value="scheduled";
  app.get("scheduleWindow").value="19:00-21:00";
  app.get("originalChk").checked=false;
  app.state.queue=[
    {id:1,status:"submitted",payload:{folder_path:"/a",folder_name:"A",images:[],title:"A",content:"A",album:"一次性A",location:"地点A",visibility:"仅自己可见",publish_mode:"immediate",original:true,tags:[]}},
    {id:2,status:"submitted",payload:{folder_path:"/b",folder_name:"B",images:[],title:"B",content:"B",album:"一次性B",location:"地点B",visibility:"仅互关好友可见",publish_mode:"immediate",original:true,tags:[]}},
  ];
  app.loadQueueItem(0);
  assert.equal(app.get("defaultAlbum").value,"一次性A");
  app.loadQueueItem(1);
  assert.equal(app.state.queuePreview.preferences.album,"账号合集");
  app.leaveQueuePreview();
  assert.equal(app.get("defaultAlbum").value,"账号合集");
  assert.equal(app.get("defaultLocation").value,"账号地点");
  assert.equal(app.get("visibility").value,"公开可见");
  assert.equal(app.get("originalChk").checked,false);
});

test("leaving queue preview preserves a manually configured platform template", () => {
  const app=workspace();
  app.get("topicTemplates").value="手动平台模板";
  app.refreshTopicTemplateOptions();
  app.get("topicTemplate").value="手动平台模板";
  app.state.queuePreview={payload:{},preferences:{topicTemplate:"手动平台模板"}};
  app.leaveQueuePreview();
  assert.equal(app.get("topicTemplate").value,"手动平台模板");
});

test("leaving queue preview preserves an old template name as unverified", () => {
  const app=workspace();
  app.get("topicTemplates").value="";
  app.state.publishingResources={};
  app.state.queuePreview={payload:{},preferences:{topicTemplate:"历史模板"}};
  app.leaveQueuePreview();
  assert.equal(app.get("topicTemplate").value,"历史模板");
  assert.match(app.get("topicTemplate").innerHTML,/\[未验证名称\] 历史模板/);
});

test("legacy queue snapshot without original uses the saved account default", () => {
  const app=workspace();
  app.state.config={original_enabled:false,default_schedule_window:"20:00-21:00",schedule_windows:["20:00-21:00"]};
  app.get("originalChk").checked=true;
  app.get("scheduleWindow").value="";
  app.state.queuePreview={payload:{},preferences:{original:undefined,scheduleWindow:""}};
  app.leaveQueuePreview();
  assert.equal(app.get("originalChk").checked,false);
  assert.equal(app.get("scheduleWindow").value,"20:00-21:00");
});

test("management verification distinguishes reviewing from published and does not claim archival", async () => {
  const app=workspace();app.setManualPending("Default",{history_id:12,title:"待核验"});
  app.context.fetch=async (url)=>{
    if(url==="/api/history/confirm") return {ok:true,json:async()=>({ok:true,status:"reviewing",reason:"平台仍在审核"})};
    if(url==="/api/history") return {ok:true,json:async()=>({ok:true,items:[]})};
    throw Error(`unexpected ${url}`);
  };
  await app.confirmPublished(12);
  assert.equal(app.state.manualPublishes.has("Default"),false);
  assert.match(app.get("runLog").textContent,/管理页已核验为审核中/);
  assert.match(app.get("runLog").textContent,/素材不会归档/);
});

test("confirmed enqueue immediately reflects the running queue and pause action", async () => {
  const app=workspace();
  app.state.current={path:"/group",selected_images:["/group/a.jpg"]};
  app.get("titleInput").value="标题";app.get("contentInput").value="正文";
  app.get("publishMode").value="scheduled";app.get("dialogTimeInput").value="2026-11-07T20:00";
  const job={id:3,status:"queued",schedule_time:"2026-11-07T20:00",payload:{}};
  app.context.fetch=async (url)=>{
    if(url==="/api/queue/enqueue") { app.get("titleInput").value="继续编辑"; return {ok:true,json:async()=>({ok:true,job})}; }
    if(url==="/api/queue") return {ok:true,json:async()=>({ok:true,items:[job],enabled:true})};
    throw Error(`unexpected ${url}`);
  };
  await app.confirmEnqueue();
  assert.equal(app.state.queueEnabled,true);
  assert.equal(app.get("pauseQueueBtn").style.display,"");
  assert.equal(app.get("startQueueBtn").style.display,"none");
  assert.equal(app.get("titleInput").value,"继续编辑");
});

test("manual queue time is never marked as an auto-replan suggestion", async () => {
  const app=workspace();
  app.state.current={path:"/group",selected_images:["/group/a.jpg"]};
  app.get("titleInput").value="标题";app.get("contentInput").value="正文";
  app.get("publishMode").value="scheduled";app.get("dialogTimeInput").value="2026-11-07T20:00";
  let queuedPayload;
  const job={id:4,status:"queued",schedule_time:"2026-11-07T20:00",payload:{}};
  app.context.fetch=async (url, options)=>{
    if(url==="/api/queue/enqueue") { queuedPayload=JSON.parse(options.body).payload; return {ok:true,json:async()=>({ok:true,job})}; }
    if(url==="/api/queue") return {ok:true,json:async()=>({ok:true,items:[job],enabled:true})};
    throw Error(`unexpected ${url}`);
  };
  await app.confirmEnqueue();
  assert.equal(queuedPayload.schedule_auto,false);
});

test("new controls are wired to real event handlers", () => {
  const app=workspace();app.bindEvents();
  for (const id of ["saveLocalTemplateBtn","deleteLocalTemplateBtn","syncResourcesBtn","testModelBtn","discardManualPublishBtn"]) {
    assert.ok(app.get(id).listeners.click?.length,`${id} should have a click handler`);
  }
  assert.ok(app.get("scheduleWindow").listeners.change?.length,"schedule window should persist its account default");
  assert.ok(app.get("originalChk").listeners.change?.length,"original declaration should persist its account default");
});

test("a delayed schedule preview cannot overwrite the next account", async () => {
  const app=workspace();app.state.activeProfile="old";app.get("publishMode").value="scheduled";app.get("scheduleWindow").value="10:00";
  let finish;app.context.fetch=()=>new Promise(resolve=>finish=resolve);
  const pending=app.previewSchedule();
  app.state.activeProfile="new";app.get("schedulePreview").value="新账号时间";
  finish({ok:true,json:async()=>({schedule_time:"2026-11-07T10:00"})});await pending;
  assert.equal(app.get("schedulePreview").value,"新账号时间");
});

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
  assert.equal(app.state.current.materialMode,"mixed");
});

test("a submitted draft cannot clear edits made while the request was pending", async () => {
  const app=workspace();app.state.current={path:"/group",selected_images:["/group/a.jpg"]};
  app.get("titleInput").value="旧标题";
  const snapshot=JSON.stringify(app.currentPayload());
  app.get("titleInput").value="新标题";
  await app.finishCurrentDraft(snapshot);
  assert.equal(app.get("titleInput").value,"新标题");
  assert.equal(app.state.current.path,"/group");
});

test("hot search uses the saved keyword for the active account", () => {
  const app=workspace();app.state.activeProfile="Profile 2";
  app.state.profiles.set("Profile 2",{manual_keywords:"艺考照，演员写真"});
  app.useProfileHotKeyword();
  assert.equal(app.get("hotKeyword").value,"艺考照");
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
  app.context.fetch = (url) => url === "/api/config" || url === "/api/profile/save"
    ? Promise.resolve({ok:true, json:async()=>({prompts:{}})})
    : new Promise((resolve) => { finish = resolve; });
  const pending = app.generateProfile();
  await new Promise(resolve => setTimeout(resolve, 0));
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
  app.context.fetch=async()=>({ok:true,json:async()=>({ok:true,update_available:false,current_version:"2026.11.10",latest_version:"2026.11.9"})});
  await app.checkForUpdates(true);
  assert.match(app.get("updateStatus").textContent,/本地 v2026\.11\.10.*GitHub 最新 v2026\.11\.9/);
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
