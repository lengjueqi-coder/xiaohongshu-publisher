(() => {
  const host = document.getElementById("pageAutomation");
  const field = (id) => document.getElementById(`auto_${id}`);
  const html = escapeHtml;
  let snapshot = null, loadedProfile = "", loadId = 0, busy = false, optionsLoaded = false, draftFx = null, loading = false;
  const styles = {praise:"夸赞型",professional:"专业展示",process:"过程分享",opinion:"观点讨论",story:"案例故事"};
  const statuses = {draft:"待审核",queued:"待提交",paused:"已暂停",submitting:"正在填入",reviewed_page:"正在提交",submitted:"平台已接收",failed:"提交前失败",unknown:"需核对平台结果",cancelled:"已取消"};
  host.innerHTML = `
    <div class="auto-heading"><div><div class="auto-kicker">PUBLISHING PLAN</div><h1>让内容按计划发生</h1><p>选好素材与规则，自动制作、排期。需要你处理时，用声音提醒。</p></div><span id="autoState" class="auto-status">读取计划中</span></div>
    <div class="auto-card"><h2>当前账号的发布计划</h2><p id="autoAccount"></p><div class="auto-grid">
      <label class="auto-wide">素材根目录<div class="auto-row"><input id="auto_root_path" placeholder="选择大文件夹，支持年份 / 月份 / 子文件夹"/><button id="autoChooseRoot">选择文件夹</button></div></label>
      <label>运行方式<select id="auto_mode"><option value="review">自动制作，先批量审核</option><option value="automatic">全自动制作并提交</option></select></label>
      <label>每天发布数量<input id="auto_daily_count" type="number" min="1" max="10"/></label>
      <label>发布时间段（每行一个）<textarea id="auto_windows" rows="2" placeholder="10:00-11:00&#10;21:00-22:00"></textarea></label>
      <label>发布时间所属时区<select id="auto_timezone"><option value="Asia/Shanghai">北京时间</option><option value="America/Los_Angeles">洛杉矶时间</option></select></label>
      <label>提前准备天数<input id="auto_ahead_days" type="number" min="1" max="7"/></label>
      <label>选图方式<select id="auto_selection"><option value="ordered">按文件顺序选取</option><option value="random">从当前子文件夹随机选择</option></select></label>
      <label>最少选几张<input id="auto_min_images" type="number" min="1" max="18"/></label>
      <label>最多选几张<input id="auto_max_images" type="number" min="1" max="18"/></label>
      <label>图片组合<select id="auto_layout"><option value="original">直接发布原图组</option><option value="2x2">四宫格封面＋原图</option><option value="3x3">九宫格封面＋原图</option><option value="collage_only">只发四宫格拼图</option></select></label>
      <label>文案来源<select id="auto_copy_mode"><option value="local">随机选择内置文案风格</option><option value="library">从我的配套文案库随机选择</option><option value="ai">根据账号画像生成 AI 文案</option></select></label>
      <div class="auto-wide"><span style="font-size:12px">参与随机的文案风格</span><div id="autoStyles" class="auto-actions">${Object.entries(styles).map(([k,v])=>`<label class="auto-check"><input type="checkbox" data-auto-style="${k}"/>${v}</label>`).join("")}</div></div>
      <label class="auto-check auto-wide"><input id="auto_allow_template_fallback" type="checkbox"/>AI 失败时允许使用内置模板；关闭时会暂停并提醒</label>
      <div class="auto-wide"><div class="auto-row"><span style="font-size:12px">我的配套文案库</span><button id="autoAddCopy">＋ 添加一组标题与正文</button></div><div id="autoLibrary"></div></div>
      <label>固定话题（空格或逗号分隔）<input id="auto_tags" placeholder="与文案中的话题合并，最多 10 个"/></label>
      <label>@ 用户（每行：昵称|用户ID）<textarea id="auto_mentions" rows="2" placeholder="可选；用于准确选择用户，避免同名误选"></textarea></label>
      <label>合集<input id="auto_album" placeholder="留空则不要求加入合集"/></label>
      <label>地点<input id="auto_location" placeholder="留空则不设置地点"/></label>
      <label>话题模板<input id="auto_topic_template" placeholder="可选"/></label>
      <label>可见范围<select id="auto_visibility"><option>仅自己可见</option><option>公开可见</option><option>仅互关好友可见</option></select></label>
      <label class="auto-check"><input id="auto_original" type="checkbox"/>声明原创</label>
      <div><button id="autoUseFx">采用右侧当前水印 / LOGO 设置</button><p id="autoFxSummary"></p></div>
      <label>每小时最多提交几条<input id="auto_submission_hour_limit" type="number" min="1" max="10"/></label>
      <label>每 24 小时最多提交几条<input id="auto_submission_day_limit" type="number" min="1" max="50"/></label>
    </div>
    <div class="auto-note">每个直接放有图片的子文件夹默认使用一次。生成任务后，图片、文案和设置会固定保存。Mac 需保持开机联网、素材磁盘可用；提交给平台的定时内容由平台继续执行。</div>
    <div class="auto-actions"><button id="autoSave" class="primary">保存规则</button><button id="autoBind">绑定当前小红书账号</button><button id="autoBuild">先制作 10 条预览</button><button id="autoEnable" class="primary">启用计划</button><button id="autoPause">暂停计划</button></div><p id="autoPlanError"></p></div>
    <div class="auto-card"><div class="auto-heading"><h2>制作与发布记录</h2><button id="autoApprove" class="primary">确认选中的草稿并提交</button></div><p>逐条展开检查图片、文案和设置。平台已接收不等于已到发布时间或通过审核。</p><div id="autoTasks"></div></div>
    <div class="auto-card"><h2>声音与语音提醒</h2><p>仅在这台 Mac 上播放，未连接手机。系统音量、勿扰模式和通知权限可能影响提醒，请先试听。</p><div class="auto-grid">
      <label class="auto-check auto-wide"><input id="auto_notify_enabled" type="checkbox"/>开启异常提醒</label>
      <label>提醒声音<select id="auto_audio"><option value="both">提示音＋中文语音</option><option value="sound">只播放提示音</option><option value="speech">只播放语音</option><option value="custom">播放我的录音</option><option value="silent">仅系统通知</option></select></label>
      <label>系统提示音<select id="auto_sound"></select></label>
      <label>中文语音<select id="auto_voice"><option>Tingting</option></select></label>
      <label>每次播放几遍<input id="auto_burst_count" type="number" min="1" max="3"/></label>
      <label class="auto-wide">语音提示内容<input id="auto_message" maxlength="200"/></label>
      <label class="auto-wide">常用语音句子<select id="auto_phrase"><option value="">自定义上方的提示内容</option><option>小红书发布助手有事项需要你处理，请查看软件通知。</option><option>小红书账号需要重新登录，请打开软件处理。</option><option>发布素材需要补充，请添加新的图片文件夹。</option></select></label>
      <label>重复方式<select id="auto_repeat"><option value="once">只提醒一次</option><option value="count">提醒指定次数后停止</option><option value="interval">按间隔持续提醒，直到我处理</option></select></label>
      <label>最多提醒几次<input id="auto_repeat_count" type="number" min="1" max="20"/></label>
      <label>再次提醒间隔<select id="auto_interval_minutes"><option value="1">每 1 分钟</option><option value="10">每 10 分钟</option><option value="60">每小时</option></select></label>
      <div><button id="autoImportAudio">导入自己的录音</button><p id="autoAudioName"></p></div>
    </div><div class="auto-actions"><button id="autoSaveNotifications">保存提醒设置</button><button id="autoPreviewAudio">试听一次</button></div></div>
    <div class="auto-card"><h2>需要处理的事项</h2><p>点击“已处理”会停止该事项的重复提醒；恢复发布计划需要单独点击“启用计划”。</p><div id="autoAlerts"></div></div>
    <div class="auto-card"><h2>Mac 后台运行</h2><label class="auto-check"><input id="auto_login_start" type="checkbox"/>登录 Mac 后启动，进程意外退出后恢复</label><br/><label class="auto-check"><input id="auto_prevent_sleep" type="checkbox"/>服务运行时防止系统因闲置而睡眠（允许屏幕关闭）</label><p>关机、手动睡眠和合盖仍会中断新任务处理。请在应用停用或迁移路径前先关闭登录启动。</p><button id="autoSaveRuntime">保存后台运行设置</button></div>
    <div id="autoFeedback" role="status" hidden></div>`;

  function feedback(message) {const el=document.getElementById("autoFeedback");el.textContent=message;el.hidden=false;log(message);}
  function confirmAction(message) {
    return new Promise(resolve=>{
      const dialog=document.createElement("dialog");dialog.className="auto-confirm";
      dialog.setAttribute("aria-label","确认操作");
      dialog.innerHTML='<h2>确认操作</h2><p></p><div class="auto-actions"><button data-confirm-no>返回</button><button class="primary" data-confirm-yes>确认</button></div>';
      dialog.querySelector("p").textContent=message;
      const finish=accepted=>{dialog.close();dialog.remove();resolve(accepted);};
      dialog.querySelector("[data-confirm-no]").onclick=()=>finish(false);
      dialog.querySelector("[data-confirm-yes]").onclick=()=>finish(true);
      dialog.oncancel=e=>{e.preventDefault();finish(false);};host.append(dialog);dialog.showModal();
    });
  }
  function addCopy(entry={}) {
    const row=document.createElement("div");row.className="auto-library-entry";
    row.innerHTML=`<label>标题<input data-copy-title maxlength="20" value="${html(entry.title||"")}"/></label><label>正文<textarea data-copy-content rows="3">${html(entry.content||"")}</textarea></label><label>配套话题<input data-copy-tags value="${html((entry.tags||[]).join(" "))}"/></label><button type="button">移除这组文案</button>`;
    row.querySelector("button").onclick=()=>row.remove();document.getElementById("autoLibrary").append(row);
  }
  const splitTags=s=>s.split(/[\s,，]+/).map(v=>v.replace(/^#/,"").trim()).filter(Boolean);
  function readConfig() {
    const config={...snapshot.plan.config};
    if(draftFx)config.fx_params=draftFx;
    ["root_path","mode","timezone","selection","layout","copy_mode","album","location","topic_template","visibility"].forEach(k=>config[k]=field(k).value.trim());
    ["daily_count","ahead_days","min_images","max_images","submission_hour_limit","submission_day_limit"].forEach(k=>config[k]=Number(field(k).value));
    config.windows=field("windows").value.split(/\n+/).map(s=>s.trim()).filter(Boolean);
    config.tags=splitTags(field("tags").value);config.mentions=field("mentions").value.split(/\n+/).map(s=>s.trim()).filter(Boolean);
    config.styles=[...host.querySelectorAll("[data-auto-style]:checked")].map(e=>e.dataset.autoStyle);
    ["original","allow_template_fallback"].forEach(k=>config[k]=field(k).checked);
    config.copy_library=[...host.querySelectorAll(".auto-library-entry")].map(e=>({title:e.querySelector("[data-copy-title]").value.trim(),content:e.querySelector("[data-copy-content]").value.trim(),tags:splitTags(e.querySelector("[data-copy-tags]").value)}));
    return config;
  }
  function fillConfig() {
    const c=snapshot.plan.config;
    draftFx=null;
    ["root_path","mode","timezone","selection","layout","copy_mode","album","location","topic_template","visibility","daily_count","ahead_days","min_images","max_images","submission_hour_limit","submission_day_limit"].forEach(k=>field(k).value=c[k]??"");
    field("windows").value=(c.windows||[]).join("\n");field("tags").value=(c.tags||[]).join(" ");field("mentions").value=(c.mentions||[]).join("\n");
    ["original","allow_template_fallback"].forEach(k=>field(k).checked=!!c[k]);
    host.querySelectorAll("[data-auto-style]").forEach(e=>e.checked=c.styles.includes(e.dataset.autoStyle));
    document.getElementById("autoLibrary").replaceChildren();(c.copy_library||[]).forEach(addCopy);
    fxSummary();
  }
  function fxSummary() {
    const fx=snapshot.plan.config.fx_params||{};
    document.getElementById("autoFxSummary").textContent=`计划已保存：${fx.logo_enabled?"LOGO 开启":"LOGO 关闭"} · ${fx.watermark_enabled?"水印开启":"水印关闭"}。改动仅影响后续新任务。`;
  }
  function fillNotifications() {
    const c=snapshot.notifications;
    field("notify_enabled").checked=!!c.enabled;
    ["audio","sound","voice","burst_count","message","repeat","repeat_count","interval_minutes"].forEach(k=>field(k).value=c[k]);
    document.getElementById("autoAudioName").textContent=c.custom_audio?`当前：${c.custom_audio.split("/").pop()}`:"未导入录音";
  }
  function renderJobs() {
    const selected=new Set([...host.querySelectorAll("[data-approve-id]:checked")].map(e=>e.dataset.approveId));
    const expanded=new Set([...host.querySelectorAll("details[open]")].map(e=>e.dataset.jobDetail));
    document.getElementById("autoTasks").innerHTML=snapshot.jobs.length?snapshot.jobs.map(j=>{
      const p=j.payload;return `<article class="auto-task"><div class="auto-row">${j.status==="draft"?`<input type="checkbox" aria-label="选择草稿 ${j.id}" data-approve-id="${j.id}" ${selected.has(String(j.id))?"checked":""}/>`:""}<h3>#${j.id} ${html(p.title||"未命名任务")}</h3><span class="auto-status">${html(statuses[j.status]||j.status)}</span></div><p>${html(p.folder_name||"")} · ${html(j.schedule_time||"立即发布")} ${html(p.timezone||"")} · ${html(p.visibility||"")}</p><details data-job-detail="${j.id}" ${expanded.has(String(j.id))?"open":""}><summary>查看图片、文案与设置</summary><div class="auto-thumbs">${(p.images||[]).map(path=>`<img src="/api/image?path=${encodeURIComponent(path)}" alt="本条已固定的图片" loading="lazy"/>`).join("")}</div><pre>${html(p.content||"")}</pre><p>${html((p.tags||[]).map(t=>"#"+t).join(" "))}</p><p>原创：${p.original?"开启":"关闭"} · 合集：${html(p.album||"未设置")} · 地点：${html(p.location||"未设置")} · @：${html((p.mentions||[]).join("、")||"无")}</p>${j.last_error?`<div class="auto-note">${html(j.last_error)}</div>`:""}<div class="auto-actions">${["draft","queued","paused","failed"].includes(j.status)?`<button data-cancel-job="${j.id}">取消并释放素材</button>`:""}${j.status==="unknown"?`<button data-resolve-job="${j.id}" data-accepted="yes">已核对：平台已接收</button><button data-resolve-job="${j.id}" data-accepted="no">已核对：平台未接收</button>`:""}</div></details></article>`;
    }).join(""):"<p>还没有任务。保存规则后，先制作一批预览。</p>";
  }
  function renderStatus() {
    const plan=snapshot.plan;const el=document.getElementById("autoState");el.className=`auto-status ${plan.state}`;el.textContent=plan.state==="attention"?"需要处理":plan.enabled?"计划运行中":"计划已暂停";
    document.getElementById("autoAccount").textContent=`${state.accounts.find(p=>p.dir===snapshot.profile_dir)?.label||snapshot.profile_dir} · ${plan.config.account_name||"尚未绑定小红书账号"}`;
    document.getElementById("autoPlanError").textContent=plan.last_error||"";
    document.getElementById("autoEnable").disabled=!!plan.enabled;document.getElementById("autoPause").disabled=!plan.enabled;
    const pending=snapshot.alerts.filter(a=>!a.acknowledged_at);const badge=document.getElementById("automationAlertBtn");badge.hidden=!pending.length;badge.textContent=`${pending.length} 项需要处理`;
    document.getElementById("autoAlerts").innerHTML=pending.length?pending.map(a=>`<div class="auto-alert"><strong>${html(a.profile_dir)}</strong> · ${html(a.created_at)}<br/>${html(a.message)}<p>已请求提醒 ${a.delivered_count} 次${a.delivery_error?` · 声音/通知失败：${html(a.delivery_error)}`:""}</p><button data-ack-alert="${a.id}">已处理，停止重复提醒</button></div>`).join(""):"<p>当前没有待处理事项。</p>";
  }
  async function loadOptions() {
    if(optionsLoaded)return;
    const o=await api("/api/notifications/options");
    field("sound").innerHTML=o.sounds.map(s=>`<option>${html(s)}</option>`).join("");
    field("voice").innerHTML=[...new Set(["Tingting",...o.voices])].map(v=>`<option>${html(v)}</option>`).join("");
    field("login_start").checked=o.runtime.login_start;field("prevent_sleep").checked=o.runtime.prevent_sleep;optionsLoaded=true;
  }
  window.loadAutomation=async function(force=false) {
    if(loading)return;loading=true;
    const id=++loadId;const profile=state.activeProfile;
    try {
      const data=await api(`/api/automation?profile_dir=${encodeURIComponent(profile)}`);if(id!==loadId||profile!==state.activeProfile||data.profile_dir!==profile)return;
      snapshot=data;await loadOptions();if(id!==loadId||profile!==state.activeProfile)return;
      if(loadedProfile!==profile||force){fillConfig();fillNotifications();loadedProfile=profile;}
      renderStatus();renderJobs();
    }catch(e){feedback(`自动发布读取失败：${e.message}`);}finally{loading=false;}
  };
  async function run(path, body={}, force=false) {
    if(busy)return;busy=true;
    const buttons=[...host.querySelectorAll("button")];buttons.forEach(b=>b.disabled=true);
    const profile=state.activeProfile;
    try {
      const data=await api(path,{...body,profile_dir:profile});
      if(profile===state.activeProfile){snapshot=data;renderStatus();renderJobs();if(force){fillConfig();fillNotifications();}}
      feedback(path==="/api/notifications/preview"?"试听播放已完成；若未听到，请检查 Mac 音量与音频输出设备。":path==="/api/notifications/save"?"提醒设置已保存。":"操作已完成。");
    }catch(e){feedback(e.message);}finally{busy=false;buttons.forEach(b=>b.disabled=false);if(snapshot)renderStatus();}
  }
  document.getElementById("autoChooseRoot").onclick=async()=>{try{const d=await api("/api/choose-folder",{});field("root_path").value=d.path;}catch(e){feedback(e.message);}};
  document.getElementById("autoAddCopy").onclick=()=>addCopy();
  document.getElementById("autoSave").onclick=()=>run("/api/automation/save",{config:readConfig()},true);
  document.getElementById("autoBind").onclick=()=>run("/api/automation/bind",{},true);
  document.getElementById("autoBuild").onclick=()=>run("/api/automation/build",{limit:10});
  document.getElementById("autoEnable").onclick=async()=>{if(snapshot?.plan.config.mode==="automatic"&&!await confirmAction(`启用“${snapshot.plan.config.account_name}”的全自动发布？\n后续符合已保存规则的任务会自动提交，当前可见范围：${snapshot.plan.config.visibility}`))return;run("/api/automation/enable",{enabled:true});};
  document.getElementById("autoPause").onclick=()=>run("/api/automation/enable",{enabled:false});
  document.getElementById("autoUseFx").onclick=()=>{draftFx=fxParamsFromUI();snapshot.plan.config.fx_params=draftFx;fxSummary();feedback("已采用当前账号的水印/LOGO；请保存规则。");};
  document.getElementById("autoApprove").onclick=async()=>{const ids=[...host.querySelectorAll("[data-approve-id]:checked")].map(e=>Number(e.dataset.approveId));if(!ids.length){feedback("请先勾选已审核的草稿。");return;}if(await confirmAction(`确认提交选中的 ${ids.length} 条内容？`))run("/api/automation/approve",{ids});};
  const readNotify=()=>({enabled:field("notify_enabled").checked,...Object.fromEntries(["audio","sound","voice","message","repeat"].map(k=>[k,field(k).value])),...Object.fromEntries(["burst_count","repeat_count","interval_minutes"].map(k=>[k,Number(field(k).value)]))});
  field("phrase").onchange=()=>{if(field("phrase").value)field("message").value=field("phrase").value;};
  document.getElementById("autoSaveNotifications").onclick=()=>run("/api/notifications/save",readNotify());
  document.getElementById("autoPreviewAudio").onclick=()=>run("/api/notifications/preview",readNotify());
  document.getElementById("autoImportAudio").onclick=()=>run("/api/notifications/import",{},true);
  document.getElementById("autoSaveRuntime").onclick=()=>run("/api/automation/runtime",{login_start:field("login_start").checked,prevent_sleep:field("prevent_sleep").checked});
  host.addEventListener("click",async e=>{
    const b=e.target.closest("button");if(!b)return;
    if(b.dataset.ackAlert)run("/api/notifications/ack",{id:Number(b.dataset.ackAlert)});
    if(b.dataset.cancelJob&&await confirmAction("取消尚未提交的任务并释放该素材？"))run("/api/automation/cancel",{id:Number(b.dataset.cancelJob)});
    if(b.dataset.resolveJob&&await confirmAction("你已经在小红书平台核对这条笔记的实际结果了吗？"))run("/api/automation/resolve",{id:Number(b.dataset.resolveJob),accepted:b.dataset.accepted==="yes"});
  });
  document.getElementById("automationAlertBtn").onclick=()=>showView("automation");
  setInterval(()=>{if(!busy)window.loadAutomation();},10000);
})();
