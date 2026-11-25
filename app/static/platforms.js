/* One shared caption, separate official settings and verification receipts. */
(() => {
  'use strict';
  const $=id=>document.getElementById(id);
  const names={xhs:'小红书',douyin:'抖音',channels:'视频号'};
  const declarations={douyin:['无需添加自主声明','内容由AI生成','内容为个人观点或见解','内容为转载信息','内容含营销推广信息','虚构演绎，仅供娱乐'],channels:['无需标注','含AI生成内容','内容为虚构剧情，仅供娱乐','个人观点，仅供参考','内容包含营销广告','内容为自行拍摄','内容为转载']};
  const defaults=p=>({declaration:declarations[p][0],publish_mode:'immediate',schedule_time:'',visibility:'public',allow_download:true,collection:'不选择合集',location:'不显示位置',music:'不添加音乐',activity:'不参与活动',hotspot:'不关联热点',short_title:'',shoot_date:'',shoot_region:'',repost_source:''});
  let saved={};try{saved=JSON.parse(localStorage.getItem('multi-platform-draft-v1')||'{}');}catch{}
  const s={active:'',kind:saved.kind||'image',shared:{title:'',content:'',tags:'',files:[],...saved.shared},settings:{douyin:{...defaults('douyin'),...saved.settings?.douyin},channels:{...defaults('channels'),...saved.settings?.channels}},bindings:{},snapshots:{},receipts:{},outcomes:saved.outcomes||{},tabs:[],busy:false,targets:new Set(['douyin']),checks:[],message:'连接官方页面后，即可同步素材、文案和发布设置。',view:'publish'};
  const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  function persist(){localStorage.setItem('multi-platform-draft-v1',JSON.stringify({kind:s.kind,shared:s.shared,settings:s.settings,outcomes:s.outcomes}));}
  function invalidate(){s.receipts={};s.checks=[];persist();}
  async function call(action,payload={}){const r=await fetch('/api/platforms/'+action,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});const data=await r.json();if(!r.ok||data.ok===false||data.error)throw Error(data.error||'操作失败');return data;}
  function payload(platform=s.active){if(platform==='xhs'){const x=currentPayload();return {media_type:s.kind,payload:{...x,title:s.shared.title,content:s.shared.content,tags:s.shared.tags.split(/[\n,，#]+/).map(v=>v.trim()).filter(Boolean),images:s.kind==='image'?[...s.shared.files]:[],video_path:s.kind==='video'?s.shared.files[0]:'',local_topic_template:'',topic_template:''}};}const settings={...s.settings[platform]};settings.shoot_region=settings.shoot_region.split('/').map(x=>x.trim()).filter(Boolean);if(s.kind==='video')delete settings.music;if(settings.publish_mode==='immediate')settings.schedule_time='';if(platform==='channels'){delete settings.allow_download;delete settings.hotspot;settings.visibility='public';if(s.kind==='video'&&!settings.short_title)settings.short_title=s.shared.title;}return {platform,media_type:s.kind,...s.shared,tags:s.shared.tags.split(/[\n,，#]+/).map(x=>x.trim()).filter(Boolean),...settings};}
  function input(key,label,ph='',extra=''){return `<label class="mp-row"><span>${label}</span><input data-field="${key}" value="${esc(s.shared[key]??s.settings[s.active][key]??'')}" placeholder="${esc(ph)}" ${extra}></label>`;}
  function picker(key,label,placeholder){return `<div class="mp-row"><span>${label}</span><div class="mp-picker"><input data-field="${key}" value="${esc(s.settings[s.active][key]||'')}" placeholder="${placeholder}"><button type="button" data-options="${key}" title="从官方页面读取选项">选择⌄</button></div></div>`;}
  function radio(key,label,options){return `<fieldset class="mp-row"><legend>${label}</legend><div class="mp-radio">${options.map(([v,t])=>`<label><input type="radio" name="mp-${key}" data-field="${key}" value="${v}" ${String(s.settings[s.active][key])===String(v)?'checked':''}>${t}</label>`).join('')}</div></fieldset>`;}
  function schedule(){return radio('publish_mode',s.active==='douyin'?'发布时间':'定时发表',[['immediate',s.active==='douyin'?'立即发布':'不定时'],['scheduled',s.active==='douyin'?'定时发布':'定时']])+`<div class="mp-row mp-schedule" ${s.settings[s.active].publish_mode!=='scheduled'?'hidden':''}><span>北京时间</span><div><input type="datetime-local" data-field="schedule_time" value="${esc(s.settings[s.active].schedule_time.replace(' ','T'))}"><small>2 小时后、14 天内；填写后核对到分钟。</small></div></div>`;}
  function upload(){return `<section class="mp-upload"><input id="mpFileInput" type="file" hidden ${s.kind==='image'?'multiple accept="image/*"':'accept="video/*"'}><button class="mp-drop" type="button" data-action="choose"><span class="mp-plus">＋</span><strong>${s.kind==='image'?'选择图片':'选择视频'}</strong><small>${s.kind==='image'?(s.active==='douyin'?'最多 35 张，单张不超过 50MB':'最多 18 张，单张不超过 20MB'):(s.active==='douyin'?'3 小时以内，单个不超过 64GB':'8 小时以内，单个不超过 20GB')}</small></button><ol class="mp-files">${s.shared.files.map((f,i)=>`<li><span>${i+1}. ${esc(f.split('/').pop())}</span><button type="button" data-remove="${i}" aria-label="移除素材">×</button></li>`).join('')}</ol><div class="mp-inline"><button type="button" data-action="upload">上传到${names[s.active]}</button><button type="button" data-action="reuse">从小红书带入素材和文案</button></div></section>`;}
  function caption(){return `${input('title',s.active==='douyin'?'作品标题':s.kind==='image'?'图文标题':'共用标题',s.active==='douyin'?'添加作品标题':s.kind==='image'?'填写标题，22 个字符内':'用作其他平台标题与本平台短标题')}<div class="mp-row mp-description"><span>${s.active==='douyin'?'作品正文':s.kind==='image'?'图文描述':'视频描述'}</span><div class="mp-editor"><textarea data-field="content" placeholder="${s.active==='douyin'?'添加作品描述…':'添加描述'}">${esc(s.shared.content)}</textarea><div class="mp-editor-foot"><button type="button" data-action="topic">#${s.active==='douyin'?'添加话题':'话题'}</button><button type="button" data-action="native" title="此项需在官方页面选择，暂不支持自动分发">@${s.active==='douyin'?'好友':'视频号'}</button><span>${s.shared.content.length} / 1000</span></div></div></div>${input('tags','话题','用逗号或 # 分隔，例如 摄影,光影')}<p class="mp-common-note">标题、正文、话题和素材在抖音、视频号之间共用。平台设置分别保存。自动核对覆盖本页所列字段；自定义封面、@、链接需在官方页面操作，尚不属于自动分发核对范围。</p>`;}
  function declaration(){return `<label class="mp-row"><span>${s.active==='douyin'?'自主声明':'视频标注'}</span><select data-field="declaration">${declarations[s.active].map(v=>`<option ${s.settings[s.active].declaration===v?'selected':''}>${esc(v)}</option>`).join('')}</select></label>${s.active==='douyin'&&s.settings.douyin.declaration==='内容为转载信息'?`<label class="mp-row"><span>取材来源</span><select data-field="repost_origin"><option value="">请选择</option><option value="external" ${s.settings.douyin.repost_origin==='external'?'selected':''}>取材站外</option><option value="internal" ${s.settings.douyin.repost_origin==='internal'?'selected':''}>取材站内（需在抖音选原作品）</option></select></label>`:''}${s.active==='channels'&&s.settings.channels.declaration==='内容为自行拍摄'?input('shoot_date','拍摄日期','','type="date"')+input('shoot_region','拍摄地点','官方名称，用 / 分隔，例如 中国 / 北京'):''}${s.active==='channels'&&s.settings.channels.declaration==='内容为转载'?input('repost_source','转载来源','填写转载来源'):''}`;}
  function render(){if(!s.active)return;const p=s.active;const connected=s.bindings[p];const snapshot=s.snapshots[p];const d=s.settings[p];
    $('pagePlatforms').innerHTML=`<div class="mp-workspace mp-${p}">
      <div class="mp-heading"><div><span>${p==='douyin'?'抖音创作者中心':'视频号 · 助手'}</span><h1>${p==='douyin'?'作品发布':s.kind==='image'?'图文管理 / 发表动态':'视频管理 / 发表动态'}</h1></div><button type="button" data-action="history">发布记录</button></div>
      <div class="mp-connect"><span class="mp-status-dot ${connected?'online':''}"></span><strong>${esc(connected?.account||'尚未连接官方发布页')}</strong><select id="mpTabs"><option value="">选择 Chrome 中的${names[p]}页面</option>${s.tabs.filter(t=>t.platform===p).map(t=>`<option value="${esc(t.client_id+':'+t.id)}" ${connected?.tab_id===t.id?'selected':''}>${esc(t.title)} · ${t.id}</option>`).join('')}</select><button type="button" data-action="refresh">刷新页面列表</button><button type="button" data-action="bind">连接</button><button type="button" data-action="open">新建发布页</button><button type="button" data-action="native">查看官方页</button></div>
      <div class="mp-tabs"><button type="button" data-kind="video" class="${s.kind==='video'?'selected':''}">发布视频</button><button type="button" data-kind="image" class="${s.kind==='image'?'selected':''}">发布图文</button></div>
      <div class="mp-body ${p==='channels'?'mp-channels-columns':''}">
        ${p==='channels'?upload():''}
        <div class="mp-form">
          <section class="mp-card"><h2>${p==='douyin'?'基础信息':'发表动态'}</h2>${p==='douyin'?upload():''}${caption()}${p==='channels'&&s.kind==='video'?input('short_title','短标题','留空时使用共用标题；6～16 个字符'):''}${p==='douyin'?`<div class="mp-row"><span>封面设置</span><div><p>默认使用首张图片${s.kind==='video'?'或视频画面':''}</p><button type="button" data-action="cover">在官方页编辑封面</button></div></div>`:''}${picker('collection',p==='douyin'?'添加合集':'添加到合集',p==='douyin'?'不选择合集':'选择合集')}${p==='douyin'?declaration():''}</section>
          <section class="mp-card"><h2>${p==='douyin'?'扩展信息':'更多设置'}</h2>${picker('location',p==='douyin'?'添加标签 · 位置':'位置',p==='douyin'?'输入相关位置':'不显示位置')}${s.kind==='image'?picker('music','选择音乐','不添加音乐'):''}${p==='douyin'?picker('hotspot','关联热点','点击输入热点词'):''}${picker('activity','官方活动','不参与活动')}${p==='channels'?`<div class="mp-row"><span>链接</span><button type="button" data-action="native">在官方页面选择链接</button></div>`:''}</section>
          <section class="mp-card"><h2>发布设置</h2>${p==='douyin'?radio('visibility','谁可以看',[['public','公开'],['friends','好友可见'],['private','仅自己可见']])+radio('allow_download','保存权限',[[true,'允许'],[false,'不允许']]):''}${schedule()}${p==='channels'?declaration():''}</section>
        </div>
        ${p==='douyin'?`<aside class="mp-preview"><h3>预览${s.kind==='image'?'图文':'视频'}</h3><div class="mp-phone"><div class="mp-preview-art">${s.kind==='image'&&s.shared.files.length?'<img alt="封面预览" src="/api/image?path='+encodeURIComponent(s.shared.files[0])+'">':s.shared.files.length?'已选择 '+s.shared.files.length+' 个素材':'素材预览'}</div><strong>${esc(s.shared.title)}</strong><p>${esc(s.shared.content)}</p><span>${esc(s.shared.tags)}</span></div><small>最终展示以官方页面为准</small></aside>`:''}
      </div>
      <div class="mp-result" role="status" aria-live="polite"><p>${esc(s.message)}</p>${s.checks.length?`<table><thead><tr><th>核对项目</th><th>结果</th><th>官方页面读回</th></tr></thead><tbody>${s.checks.map(c=>`<tr class="${c.ok?'pass':'fail'}"><td>${esc(c.field)}</td><td>${c.ok?'✓ 一致':'需处理'}</td><td>${esc(Array.isArray(c.actual)?c.actual.join('、'):c.actual)}</td></tr>`).join('')}</tbody></table>`:''}</div>
      <footer class="mp-footer"><div class="mp-targets"><span>分发到</span>${['xhs','douyin','channels'].map(t=>`<label title=""><input type="checkbox" data-target="${t}" ${s.targets.has(t)?'checked':''} >${names[t]}</label>`).join('')}</div><button type="button" data-action="fill">填写并逐项核对</button><button type="button" data-action="verify">读取官方页核对</button><button type="button" class="mp-primary" data-action="publish">${s.targets.size>1?'确认并分发':'发布'}</button></footer>
      <dialog id="mpOptions"><header><strong id="mpOptionsTitle">选择官方选项</strong><button type="button" data-close>×</button></header><div class="mp-option-search"><input id="mpOptionQuery" placeholder="搜索官方选项"><button type="button" id="mpOptionSearch">搜索</button></div><div id="mpOptionList"></div></dialog>
    </div>`;
    $('pagePlatforms').querySelectorAll('button,input,select,textarea').forEach(e=>e.disabled=s.busy||e.hasAttribute('data-unavailable'));
    bindUI();window.distributionUI?.attach();
  }
  async function run(task){if(s.busy)return;s.busy=true;render();try{await task();}catch(e){s.message=e.message;}finally{s.busy=false;render();}}
  function takeSnapshot(platform,data){s.snapshots[platform]=data.snapshot;if(data.binding)s.bindings[platform]=data.binding;}
  async function refresh(){const r=await call('tabs');s.tabs=r.tabs;if(r.bindings)s.bindings=r.bindings;}
  async function doFill(platform,frozen=payload(platform)){const r=await call(platform==='xhs'?'xhs-prepare':'prepare',frozen);if(platform!=='xhs')takeSnapshot(platform,r);s.checks=r.checks||[];if(r.verified)s.receipts[platform]={id:r.receipt,payload:JSON.stringify(frozen)};else delete s.receipts[platform];s.message=r.verified?`${names[platform]}：逐项核对通过，尚未发布。`:`${names[platform]}：存在不一致，已停止。`;return r;}
  async function options(field){if(s.busy)return;s.busy=true;render();let r;try{r=await call('options',{platform:s.active,field});}finally{s.busy=false;render();}const dialog=$('mpOptions');$('mpOptionsTitle').textContent='选择官方'+({collection:'合集',music:'音乐',location:'位置',activity:'活动',hotspot:'热点'}[field]);const list=items=>{$('mpOptionList').replaceChildren();if(!items.length)$('mpOptionList').textContent='官方页面没有可用选项，可搜索或在官方页面完成后读取核对。';items.forEach(item=>{const b=document.createElement('button');b.type='button';b.textContent=item.detail||item.label;b.onclick=()=>{s.settings[s.active][field]=item.label;invalidate();dialog.close();render();};$('mpOptionList').append(b);});};list(r.options);$('mpOptionSearch').onclick=async()=>{try{const result=await call('options',{platform:s.active,field,query:$('mpOptionQuery').value});list(result.options);}catch(e){$('mpOptionList').textContent=e.message;}};dialog.showModal();}
  function bindUI(){const root=$('pagePlatforms');
    root.querySelectorAll('[data-field]').forEach(e=>e.addEventListener('input',()=>{const key=e.dataset.field;let value=e.type==='radio'&&key==='allow_download'?e.value==='true':e.value;if(['title','content','tags'].includes(key))s.shared[key]=value;else s.settings[s.active][key]=value;invalidate();if(key==='content'){const counter=root.querySelector('.mp-editor-foot>span');if(counter)counter.textContent=value.length+' / 1000';}if(key==='publish_mode'||key==='declaration')render();}));
    root.querySelectorAll('[data-kind]').forEach(e=>e.onclick=()=>{if(s.kind===e.dataset.kind)return;s.kind=e.dataset.kind;s.shared.files=[];invalidate();s.message='已切换素材类型，请新建对应的官方发布页后上传。';render();});
    root.querySelectorAll('[data-target]').forEach(e=>e.onchange=()=>{if(e.checked)s.targets.add(e.dataset.target);else s.targets.delete(e.dataset.target);render();});
    root.querySelectorAll('[data-remove]').forEach(e=>e.onclick=()=>{s.shared.files.splice(Number(e.dataset.remove),1);invalidate();render();});
    root.querySelectorAll('[data-options]').forEach(e=>e.onclick=()=>options(e.dataset.options).catch(err=>{s.message=err.message;render();}));
    root.querySelector('[data-close]').onclick=()=>$('mpOptions').close();
    root.querySelectorAll('[data-action]').forEach(e=>e.onclick=()=>{
      const action=e.dataset.action;
      if(action==='topic'){root.querySelector('[data-field=tags]').focus();return;}
      if(action==='reuse'){
        const x=typeof currentPayload==='function'?currentPayload():null;
        if(!x){s.message='请先在小红书选择素材并填写文案。';render();return;}
        s.shared={title:x.title||'',content:x.content||'',tags:(x.tags||[]).join(', '),files:x.images||[]};invalidate();s.message='已带入小红书素材、文案和话题；新平台设置请分别确认。';render();return;
      }
      if(action==='publish'){
        const targets=[...s.targets];if(!targets.length){s.message='请选择分发平台。';render();return;}
        const profile=state.activeProfile;
        const frozen=Object.fromEntries(targets.map(t=>[t,payload(t)]));
        run(async()=>{
          const accounts=await call('tabs');if(accounts.bindings)s.bindings=accounts.bindings;
          if(profile!==state.activeProfile)throw Error('账号已切换，请重新核对这份稿件。');
          const reviewed=reviewAccounts(targets,s.bindings,profile);
          const summary=targets.map(t=>{const d=t==='xhs'?frozen[t].payload:frozen[t];const visibility=t==='xhs'?d.visibility:({public:'公开',private:'仅自己可见',friends:'好友可见'}[d.visibility]);return [names[t],reviewed[t].account,reviewed[t].account_id,visibility,d.publish_mode==='scheduled'?'北京时间 '+d.schedule_time:'立即发布'].join(' · ');});
          if(!window.confirm('确认发布这一份内容？\n'+summary.join('\n')+'\n\n确认后由后台执行；关闭工作台可在分发记录恢复查看。'))return;
          const result=await createReviewed({profile_dir:profile,targets:frozen,expected_accounts:accountIds(reviewed)},false);
          s.message='已保存后台分发任务 '+result.batches[0].id.slice(0,8)+'，请在分发记录查看逐个平台的结果。';
          window.distributionUI?.showRecords();
        });return;
      }
      // Read current select before render() rebuilds the form.
      const selected=$('mpTabs')?.value;
      run(async()=>{
        if(action==='refresh'){await refresh();s.message='请选择对应账号的官方页面并点击连接。';}
        if(action==='bind'){if(!selected)throw Error('请选择一个官方页面');const [client,id]=selected.split(':');const r=await call('bind',{platform:s.active,client_id:client,tab_id:Number(id)});takeSnapshot(s.active,r);invalidate();s.message='已连接 '+(r.binding.account||names[s.active])+'。';}
        if(action==='open'){const r=await call('open',{platform:s.active,media_type:s.kind,client_id:s.bindings[s.active]?.client_id});takeSnapshot(s.active,r);await refresh();invalidate();s.message='已新建官方发布页，可上传素材。';}
        if(action==='native'||action==='cover'){await call('focus',{platform:s.active});s.message=action==='cover'?'请在官方页编辑封面；返回后点击“读取官方页核对”。':'官方页面已打开，返回后可重新核对填写结果。';}
        if(action==='choose'){const r=await call('choose-files',{media_type:s.kind});if(r.files.length){s.shared.files=r.files;invalidate();s.message='已选择素材，请上传到对应平台。';}}
        if(action==='upload'){if(!s.shared.files.length)throw Error('请先选择素材');s.message='上传中…';const r=await call('upload',{platform:s.active,media_type:s.kind,files:s.shared.files});takeSnapshot(s.active,r);invalidate();s.message='素材上传完成，接下来填写文案和设置。';}
        if(action==='fill')await doFill(s.active);
        if(action==='verify'){delete s.receipts[s.active];const r=await call('verify',payload());takeSnapshot(s.active,r);s.checks=r.checks;if(r.verified)s.receipts[s.active]={id:r.receipt,payload:JSON.stringify(payload())};s.message=r.verified?'官方页面逐项核对通过。':'存在不一致，请查看核对结果。';}
        if(action==='history'){window.distributionUI?.showRecords();const r=await call('history');s.message=r.history.length?r.history.map(x=>`${names[x.platform]} · ${x.title} · ${({submitted:'已提交，待核验',submitting:'提交中，待核验',unknown:'结果待核对'}[x.status]||x.status)}\n${x.message}`).join('\n'):'尚无多平台分发提交记录。';}
      });
    });
  }
  function show(){if(!s.active)return;$('appShell').classList.add('platform-view');$('appShell').classList.remove('automation-view');['Publish','Records','Settings','Profile','Automation','Hot'].forEach(n=>{const e=$('page'+n);if(e)e.style.display='none';});$('pagePlatforms').hidden=false;$('bottomBar').hidden=true;render();}
  function suspend(){$('pagePlatforms').hidden=true;$('appShell').classList.remove('platform-view');}
  function select(platform){if(s.busy)return;document.body.dataset.publishingPlatform=platform;if(platform==='xhs'){s.active='';suspend();$('platformBtn').querySelector('.xhs-badge').textContent='小红书';showView('publish');}else{const previous=s.active;s.active=platform;if(s.targets.size===1&&(!previous||s.targets.has(previous)))s.targets=new Set([platform]);$('platformBtn').querySelector('.xhs-badge').textContent=names[platform];show();refresh().then(render).catch(e=>{s.message=e.message;render();});}$('platformMenu').style.display='none';document.querySelectorAll('[data-platform]').forEach(e=>e.classList.toggle('active',e.dataset.platform===platform));}
  let xhsVideo = '';
  function reviewAccounts(targets,bindings,profile){
    const identity=state.accounts.find(a=>a.dir===profile)?.identity;
    return Object.fromEntries(targets.map(t=>{
      const account=t==='xhs'?{account:identity?.nickname,account_id:identity?.user_id}:bindings[t];
      if(!account?.account_id)throw Error('请先核实'+names[t]+'的发布账号');
      return [t,{account:account.account,account_id:account.account_id}];
    }));
  }
  function accountIds(reviewed){
    return Object.fromEntries(Object.entries(reviewed).map(([t,b])=>[t,b.account_id]));
  }
  async function createReviewed(body,prepareOnly){
    const bytes=new TextEncoder().encode(JSON.stringify(body));
    const hash=Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',bytes)),n=>n.toString(16).padStart(2,'0')).join('');
    const key='distribution-confirmation:'+hash;
    const id=sessionStorage.getItem(key)||crypto.randomUUID();
    sessionStorage.setItem(key,id);
    const response=await fetch('/api/distribution/create',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...body,prepare_only:prepareOnly,request_id:id})});
    const result=await response.json();
    if(!response.ok||result.error)throw Error(result.error||'分发任务创建失败');
    if(result.batches[0].state==='cancelled')sessionStorage.removeItem(key);
    return result;
  }
  const declarationsFromXhs = {
    '笔记含AI合成内容': ['内容由AI生成','含AI生成内容'],
    '虚构演绎，仅供娱乐': ['虚构演绎，仅供娱乐','内容为虚构剧情，仅供娱乐'],
    '内容包含营销广告': ['内容含营销推广信息','内容包含营销广告'],
    '内容来源声明：来源转载': ['内容为转载信息','内容为转载'],
    '内容来源声明：自主拍摄': [null,'内容为自行拍摄']
  };
  async function chooseXhsVideo(){
    try {
      const {files} = await call('choose-files',{media_type:'video'});
      if(files.length){xhsVideo=files[0];$('xhsVideoName').textContent=xhsVideo.split('/').pop();}
    } catch(error){window.alert(error.message);}
  }
  async function fromXhs(targetNames,x,video=false,prepareOnly=false){
    if(s.busy)return;
    s.busy=true;
    try {
      const kind=video?'video':'image';
      const version=await (await fetch('/api/version',{cache:'no-store'})).json();
      if(!version.xhs_sync_review)throw Error('请重新启动发布助手，加载支持同步核对的后台。');
      const files=video?(xhsVideo?[xhsVideo]:[]):x.images;
      if(!files.length)throw Error(video?'请先选择视频':'请先选择图片');
      const targets={xhs:{media_type:kind,payload:{...x,images:video?[]:files,video_path:video?xhsVideo:''}}};
      if(video)delete targets.xhs.payload.allow_copy;
      const warnings=[];
      const visibility={'公开可见':'public','仅自己可见':'private','仅互关好友可见':'friends'}[x.visibility];
      if(!visibility)throw Error('请重新选择小红书可见范围');
      const {bindings}=await call('tabs');
      const reviewed=reviewAccounts(['xhs',...targetNames],bindings,x.profile_dir);
      const summary=[['小红书',reviewed.xhs.account,reviewed.xhs.account_id,x.visibility].join(' · ')];
      for(const platform of targetNames){
        const binding=bindings[platform];
        if(!binding?.account_id)throw Error('请在'+names[platform]+'工作台连接已登录的官方页面');
        const dy=platform==='douyin';
        const d={...s.settings[platform],platform,media_type:kind,files,title:x.title,content:x.content,tags:x.tags,
          publish_mode:x.publish_mode,schedule_time:x.schedule_time,collection:x.album||'不选择合集',
          location:x.location||'不显示位置',visibility:dy?visibility:'public',
          declaration:declarationsFromXhs[x.declaration]?.[dy?0:1]||declarations[platform][0]};
        d.shoot_region=(d.shoot_region||'').split('/').map(v=>v.trim()).filter(Boolean);
        if(dy&&d.declaration==='内容为转载信息'){
          if(x.repost_origin!=='external')throw Error('请在小红书转载设置选择抖音取材来源。站内转载需到抖音关联原作品。');
          d.repost_origin=x.repost_origin;
        }
        if(video)delete d.music;
        if(!dy){
          delete d.hotspot;delete d.allow_download;
          if(video)d.short_title=x.title.length<=16&&x.title.length>=6?x.title:d.short_title;
          if(d.declaration==='内容为转载')d.repost_source=x.repost_source;
          if(d.declaration==='内容为自行拍摄')d.shoot_date=x.shooting_date||d.shoot_date;
          if(visibility!=='public')warnings.push('视频号只能公开发表；确认即同意在视频号公开此稿。');
        }
        if(x.declaration&&!declarationsFromXhs[x.declaration]?.[dy?0:1])warnings.push(names[platform]+'没有对应的“'+x.declaration+'”选项。');
        targets[platform]=d;
        summary.push([names[platform],binding.account,binding.account_id,dy?{'public':'公开','private':'仅自己可见','friends':'好友可见'}[visibility]:'公开',
          d.collection,d.location,d.declaration,dy&&d.declaration==='内容为转载信息'?'取材站外':'',d.activity,d.music,d.hotspot,dy?(d.allow_download?'允许下载':'禁止下载'):'',video&&!dy?'短标题：'+(d.short_title||'留空'):''].filter(Boolean).join(' · '));
      }
      if(targetNames.length&&(x.mentions.length||x.activities.length||x.original||!x.allow_copy))warnings.push('小红书的 @、关联活动、原创和正文复制设置仅用于小红书。');
      const caption=[(prepareOnly?'填写并停在提交前':'确认发布')+'《'+x.title+'》',x.schedule_time||'立即发布',...summary,...warnings].join('\n');
      if(!window.confirm(caption))return;
      const body={profile_dir:x.profile_dir,sync_from:'xhs',targets,
        expected_accounts:accountIds(reviewed)};
      const result=await createReviewed(body,prepareOnly);
      const batch=result.batches[0];
      if(!prepareOnly&&batch.prepare_only&&batch.state==='paused'){
        const resume=await fetch('/api/distribution/resume',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:batch.id,remaining_only:true})});
        if(!resume.ok)throw Error((await resume.json()).error||'恢复分发失败');
      }
      await window.distributionUI.showRecords();
    } finally{s.busy=false;}
  }
  window.platformWorkspace={get active(){return s.active;},get busy(){return s.busy;},show,suspend,select,history(){show();document.querySelector('[data-action=history]').click();},video(){s.kind='video';s.shared.files=[];s.targets=new Set(['xhs','douyin']);select('douyin');},getSettings(){return JSON.parse(JSON.stringify(s.settings));},getMaterial(){return {files:[...s.shared.files],kind:s.kind};},applyCopy(copy){s.shared.title=copy.title;s.shared.content=copy.content;if(copy.short_title)s.settings.channels.short_title=copy.short_title;invalidate();render();},chooseXhsVideo,fromXhs};
  document.querySelectorAll('[data-platform]').forEach(e=>e.addEventListener('click',()=>select(e.dataset.platform)));
})();
