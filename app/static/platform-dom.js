async function officialPage(p) {
  'use strict';
  const shadow = document.querySelector('wujie-app')?.shadowRoot;
  const root = p.platform === 'channels' ? (shadow || document) : document;
  const all = (s,scope=root)=>Array.from(scope.querySelectorAll(s));
  const visible=e=>!!e && e.getClientRects().length>0 && getComputedStyle(e).visibility!=='hidden';
  const clean=s=>String(s||'').replace(/[\u200b\ufeff]/g,'').replace(/\u00a0/g,' ').trim();
  const text=e=>clean(e?.innerText ?? e?.textContent);
  const one=(xs,name)=>{if(xs.length!==1)throw Error(name+'：官方控件'+(xs.length?'不唯一':'未找到'));return xs[0];};
  const vis=s=>all(s).filter(visible);
  const chLabels={location:'位置',collection:'添加到合集',activity:'活动',music:'音乐',declaration:'视频标注',link:'链接'};
  function fieldElement(field) {
    if (field==='title') return vis('input[placeholder*="作品标题"],input[placeholder*="填写标题"]').at(0);
    if (field==='short_title') return vis('input[placeholder*="短标题"]').at(0);
    if (field==='schedule_time') return vis('input[placeholder="日期和时间"],input[placeholder="请选择发表时间"]').at(0);
    if (p.platform==='channels') {
      const row=vis('.form-item').find(e=>text(e.querySelector('.label'))===chLabels[field]);
      const selectors={location:'.position-display',collection:'.post-album-display',activity:'.activity-display',declaration:'.mark-tag-select',music:'.link-display-wrap',link:'.link-display-wrap'};
      return row?.querySelector(selectors[field]||'.form-item-body') || row?.querySelector('.form-item-body');
    }
    const selectors={declaration:'[class^="selectBox-"]',collection:'.semi-select',location:'.semi-select-filterable',hotspot:'.semi-select-filterable',music:'[class*="music"]'};
    if (field==='collection') return vis('.semi-select').find(e=>!e.className.includes('select-mix-type')&&!e.className.includes('filterable')&&text(e)!=='位置');
    if (field==='location') return vis('.semi-select-filterable').at(0);
    if (field==='hotspot') return vis('.semi-select-filterable').find(e=>text(e).includes('热点')) || vis('.semi-select-filterable').at(-1);
    if (field==='music') return vis('[class^=action-]').find(e=>/选择音乐|更换音乐|修改音乐/.test(text(e)));
    return vis(selectors[field]||'[data-never-match]').at(0);
  }
  function readField(field) {
    if(p.platform==='douyin'&&field==='activity'){
      const tokens=vis('[data-mention=activity]').map(e=>text(e).toLowerCase());
      if(!tokens.length)return '不参与活动';
      const cards=vis('[class*=activity-right]');
      const matched=cards.filter(e=>tokens.includes(text(e.querySelector('[class^=title-]')).toLowerCase()));
      const selected=matched.length===tokens.length?matched:cards.filter(e=>text(e.querySelector('[class^=added-]'))==='已添加');
      return selected.map(e=>text(e.querySelector('[class^=title-]'))).join('、')||tokens.join('、');
    }
    const e=fieldElement(field);
    if (p.platform==='douyin'&&field==='music') {const name=vis('[class^=sub-desc-title-]').at(0);return name?text(name):'不添加音乐';}
    if (!e) return null;
    if ('value' in e) return e.value;
    if(field==='collection'&&/^(请选择合集|选择合集|不选择合集)$/.test(text(e)))return '不选择合集';
    if(p.platform==='douyin'&&field==='location'&&(!text(e)||/^输入/.test(text(e))))return '不显示位置';
    if(p.platform==='douyin'&&field==='hotspot'&&(!text(e)||/^点击输入/.test(text(e))))return '不关联热点';
    if (p.platform==='channels') {
      const sel={location:'.location-name',collection:'.display-text',declaration:'.select-display',activity:'.activity-display-wrap',music:'.music-name,.display-text'}[field];
      if (sel && e.querySelector(sel)) return text(e.querySelector(sel));
      if (field==='location') return text(e);
      if (field==='declaration') return text(e.querySelector('.select-display'));
      if (field==='music' && text(e)==='选择背景音乐') return '不添加音乐';
    }
    return text(e);
  }
  function radio(value) {return vis('label').filter(e=>text(e)===value&&e.querySelector('input[type=radio],input[type=checkbox]'));}
  function findTarget() {
    if (p.radio) return one(radio(p.radio),p.radio);
    if (p.selector) {
      let es=vis(p.selector); if(p.text!==undefined)es=es.filter(e=>text(e)===p.text); if(p.number!==undefined)es=es.filter(e=>parseInt(text(e),10)===p.number);
      return p.index!==undefined ? es[p.index] : one(es,p.selector);
    }
    if (p.option!==undefined) {
      if(p.platform==='douyin'&&p.field==='music') {const item=one(vis('[class^=song-name]').filter(e=>text(e)===p.option),'音乐');return item.closest('[class^=card-wrapper]').querySelector('button');}
      let es;
      if (p.platform==='channels') {
        const scopes={location:'.location-filter-wrap',collection:'.post-album-wrap .filter-wrap',activity:'.activity-filter-wrap',declaration:'.mark-tag-options',music:'.post-with-link:has(.bgm-form-content-wrap) .link-list-options'};
        const scope=root.querySelector(scopes[p.field]||'body')||root;
        es=all('.option-item,.mark-tag-option,.bgm-item-wrap,.no-add-wrap,[role=option],li',scope).filter(visible).filter(e=>text(e)===p.option||text(e.querySelector('.name,.option-main,.song-info .title,.wordings'))===p.option);
      } else {
        es=vis('.semi-select-option,[role=option],label').filter(e=>text(e)===p.option||text(e).split('\n')[0]===p.option);
        if(!es.length)es=vis('div,span').filter(e=>!e.children.length&&text(e)===p.option);
      }
      return one(es,p.option);
    }
    if (p.button) {
      let scope=root;
      if(p.dialog) {
        const dialogs=vis('[role=dialog],.semi-modal-content,.weui-desktop-dialog');
        if(dialogs.length)scope=dialogs.at(-1);
      }
      return one(all('button',scope).filter(visible).filter(e=>text(e)===p.button),p.button);
    }
    if (p.field==='content') {
      const e=editor(),leaf=Array.from(e.querySelectorAll('[data-string]:not([data-enter])')).find(n=>clean(n.textContent)&&n.getBoundingClientRect().width>0);
      return leaf||e;
    }
    if (p.field) {const e=fieldElement(p.field);if(!e)throw Error('找不到官方设置：'+p.field);return e;}
    throw Error('无效的控件');
  }
  function editor(){return one(vis('[contenteditable]').filter(e=>e.getAttribute('contenteditable')!=='false'),'作品描述');}
  function inputValue(e,value,blur=true) {
    if(!e)throw Error('找不到输入框');
    if(e.readOnly)throw Error('此选项需要通过官方选择器填写');
    const win=e.ownerDocument.defaultView||window;
    Object.getOwnPropertyDescriptor(e.tagName==='TEXTAREA'?win.HTMLTextAreaElement.prototype:win.HTMLInputElement.prototype,'value').set.call(e,value);
    e.dispatchEvent(new win.Event('input',{bubbles:true,composed:true}));
    e.dispatchEvent(new win.Event('change',{bubbles:true,composed:true}));
    if(blur)e.blur();
  }
  switch(p.action) {
    case 'close_music_picker': {
      const panel=vis('.semi-sidesheet-content').find(e=>text(e).startsWith('选择音乐'));
      if(!panel)return false;
      one(all('button.semi-sidesheet-close',panel).filter(visible),'关闭音乐选择').click();
      return true;
    }
    case 'navigate_empty_channels': {
      const allowed=['/platform/post/create','/platform/post/finderNewLifeCreate'];
      if(p.platform!=='channels'||!allowed.includes(p.path))throw Error('无效的官方发布页');
      if(vis('.thumb-item,video').length)throw Error('官方页已有素材，请保留草稿并另开发布页');
      const ed=vis('[contenteditable]').find(e=>e.getAttribute('contenteditable')!=='false');
      if(ed&&clean(ed.innerText))throw Error('官方页已有文案，请保留草稿并另开发布页');
      if(['title','short_title'].some(key=>clean(readField(key))))throw Error('官方页已有标题，请保留草稿并另开发布页');
      const app=document.querySelector('#app')?.__vue__;
      if(!app?.$router)throw Error('未找到官方导航，请重新连接发布页');
      if(app.$route?.path!==p.path)await app.$router.push(p.path);
      return true;
    }
    case 'clear_field': {
      const e=fieldElement(p.field);
      if(p.platform==='douyin'&&p.field==='music') {
        const card=root.querySelector('[class^=sub-desc-title-]')?.closest('[class*=container-del-]');
        one(card?all('[class^=del-]',card):[],'删除音乐').click();return true;
      }
      const clear=e?.querySelector('.semi-select-clear,.delete-icon,.remove-icon,.clear-icon');
      if(!clear)throw Error('官方页面未找到清空'+p.field+'的控件，请在官方页清空后重新核对');
      clear.click();return true;
    }
    case 'set_activity': {
      const cards=vis('[class*=activity-right]');
      const target=p.value==='不参与活动'?null:one(cards.filter(e=>text(e.querySelector('[class^=title-]'))===p.value),'官方活动');
      for(const c of cards){if(c!==target&&text(c.querySelector('[class^=added-]'))==='已添加')c.querySelector('[class^=added-]').click();}
      if(target&&readField('activity')!==p.value)target.querySelector('[class^=title-]').click();return true;
    }
    case 'search_option': {
      if(p.platform==='douyin'&&['location','hotspot'].includes(p.field)) {inputValue(fieldElement(p.field)?.querySelector('input'),p.value,false);return true;}
      const selectors=p.platform==='channels'?{location:'input[placeholder="搜索附近位置"]',activity:'input[placeholder="搜索活动"]',music:'input[placeholder*="搜索歌名"]'}:{music:'input[placeholder*="搜索"]'};
      const es=vis(selectors[p.field]||'[data-never-match]');const box=one(es,'搜索');box.focus();inputValue(box,p.value,false);return true;
    }
    case 'activate': {
      const e=findTarget();
      if(p.field==='music'&&p.platform==='channels'&&e.matches('.bgm-item-wrap')){
        e.dispatchEvent(new MouseEvent('mouseenter'));
        await Promise.resolve();
        one(all('button',e).filter(b=>text(b)==='选择'),'选择此曲').click();
      } else if(p.platform==='channels'&&p.field==='music') {
        (e.querySelector('.wordings')||e).click();
      } else e.click();
      return true;
    }
    case 'exists':return vis(p.selector).length>0;
    case 'calendar_month': {const es=vis('.weui-desktop-picker__panel__label');return es.slice(0,2).map(e=>parseInt(text(e),10));}
    case 'point': {
      const e=findTarget(); if(!e)throw Error('控件未找到');
      if(e.disabled||e.getAttribute('aria-disabled')==='true'||/btn_disabled|option-disabled/.test(e.className))throw Error('官方控件不可用：'+text(e));
      e.scrollIntoView({block:'center',inline:'nearest',behavior:'instant'});
      const b=e.getBoundingClientRect();
      const left=Math.max(0,b.left),right=Math.min(innerWidth,b.right),top=Math.max(0,b.top),bottom=Math.min(innerHeight,b.bottom);
      if(right<=left||bottom<=top)throw Error('官方控件位于可见区域外，请加宽浏览器窗口后重新核对');
      const x=(left+right)/2,y=(top+bottom)/2;
      const hit=root.elementFromPoint?.(x,y);
      if(!hit||!e.contains(hit))throw Error('官方控件被其他界面遮挡，请先处理官方页面再重新核对');
      return {x,y};
    }
    case 'text':inputValue(p.selector?one(vis(p.selector),p.selector):fieldElement(p.field),p.value);return true;
    case 'focus_editor': {
      const e=editor();e.focus();
      if(p.clear){const selection=window.getSelection();const range=document.createRange();range.selectNodeContents(e);selection.removeAllRanges();selection.addRange(range);}
      return true;
    }
    case 'blur_editor':editor().blur();return true;
    case 'external_repost_selected': {
      const dialog=vis('.semi-modal-content').find(e=>text(e).includes('对作品内容添加声明'));
      if(!dialog)return false;
      const selected=all('label.semi-radio-checked',dialog).map(text);
      return selected.includes('取材站外') && /作者声明[：:]\s*内容来源于\s*站外/.test(text(dialog));
    }
    case 'radio_checked':return radio(p.value).some(e=>e.querySelector('input').checked);
    case 'read_field':return readField(p.field);
    case 'topic_exists':return vis('[class*=tag-hash-view-name]').some(e=>text(e)===p.value);
    case 'options': {
      if(p.platform==='douyin'&&p.field==='activity')return [{label:'不参与活动',detail:'不参与活动'},...vis('[class*=activity-right]').map(e=>({label:text(e.querySelector('[class^=title-]')),detail:text(e)}))];
      if(p.platform==='douyin'&&p.field==='music')return vis('[class^=song-name]').map(e=>({label:text(e),detail:text(e.closest('[class^=song-info]'))}));
      const selectors=p.platform==='channels'?'.option-item,.mark-tag-option,.bgm-item-wrap,.no-add-wrap':'.semi-select-option,[role=option]';
      const scopes={location:'.location-filter-wrap',collection:'.post-album-wrap .filter-wrap',activity:'.activity-filter-wrap',declaration:'.mark-tag-options',music:'.post-with-link:has(.bgm-form-content-wrap) .link-list-options'};
      const scope=p.platform==='channels'&&scopes[p.field]?root.querySelector(scopes[p.field]):root;
      return all(selectors,scope||root).filter(visible).filter(e=>e.getAttribute('aria-disabled')!=='true'&&!e.classList.contains('semi-select-option-disabled')).map(e=>({label:text(e.querySelector('.name,.option-main,.song-info .title,.wordings'))||text(e).split('\n')[0],detail:text(e)})).filter(e=>e.label&&!/^(未搜索到相关位置|暂无数据|暂无结果|加载中[.…]*)$/.test(e.label));
    }
    case 'snapshot': {
      const body=root.querySelector('body')||root.body||root;
      const radios=vis('label').filter(e=>e.querySelector('input:checked')).map(text);
      const fields={}; for(const key of ['title','short_title','schedule_time','location','collection','declaration','activity','music','hotspot'])fields[key]=readField(key);
      const ed=vis('[contenteditable]').find(e=>e.getAttribute('contenteditable')!=='false');
      fields.declaration_detail=text(root.querySelector('.mark-tag-summary'));
      fields.content=ed?clean(ed.innerText):null;
      if(ed&&p.platform==='douyin'&&ed.querySelector('[data-mention=activity]'))fields.content=all('.ace-line',ed).map(line=>{const copy=line.cloneNode(true);copy.querySelectorAll('[data-mention=activity]').forEach(e=>e.remove());return clean(copy.textContent);}).join('\n');
      fields.tags=ed?all('[data-mention="#"],[data-type=topic],a',ed).map(text):[];
      const images=p.platform==='douyin'?vis('div[draggable=true][class*="img-"]'):vis('.thumb-item');
      const video=vis('video').at(0);
      const media=images.map(e=>{const bg=getComputedStyle(e).backgroundImage;return bg&&bg!=='none'?bg:(e.querySelector('img')?.getAttribute('src')||'');});
      const media_ready=images.length>0&&images.every((e,i)=>media[i]&&!/^(none|data:)/.test(media[i])&&!e.querySelector('[class*=loading],[class*=progress]')&&Array.from(e.querySelectorAll('img')).every(img=>img.complete&&img.naturalWidth>0));
      const errors=vis('.semi-form-field-error-message,.weui-desktop-form__tips_error').map(text).filter(Boolean);
      errors.push(...vis('[role=alert],.semi-toast,.weui-desktop-toast').map(text).filter(t=>/失败|错误|不支持|不可|超过|限制|频繁|验证|未完成/.test(t)));
      const risky=vis('iframe[src*=captcha],[class*=captcha],.verify-dialog').length>0||errors.some(t=>/安全验证|验证码|频繁|账号异常|限制发布|请先登录/.test(t))||/为确保是本人操作|接收短信验证码|选择其他验证方式/.test(text(body));
      const buttons=vis('button').map(e=>({text:text(e),disabled:!!e.disabled||/btn_disabled/.test(e.className)}));
      const account=p.platform==='douyin'?(text(document.querySelector('[class*=author-name]'))||text(document.querySelector('[class*=nickname]'))||document.body.innerText.match(/(?:预览图文|预览封面\/标题)\n@([^\n]+)/)?.[1]||'') : text(document.querySelector('.account-info .name'));
      return {url:location.origin+location.pathname,media_type:location.pathname.includes('image')||location.pathname.includes('NewLifeCreate')?'image':'video',ready:!!ed,account,fields,radios,image_count:images.length,media,media_ready,video:video?{src:video.currentSrc||video.src,duration:Number.isFinite(video.duration)?video.duration:null}:null,errors,risky,buttons,has_private:vis('label').some(e=>text(e)==='仅自己可见'),summary:text(body).slice(0,8500)};
    }
    case 'management': {
      const cards=p.platform==='channels'?vis('.post-feed-item,.feed-item,.feed-card'):vis('[class^=video-card-content-],[class*=content-item],[class*=content-card]');
      return cards.map(e=>{
        let post=e.__vue__?.$props?.post||{};
        if(p.platform==='douyin'){
          let fiber=e[Object.keys(e).find(k=>k.startsWith('__reactFiber'))];
          for(let i=0;fiber&&i<6;i++,fiber=fiber.return){
            const data=fiber.memoizedProps?.data;
            if(data?.aweme_id){post=data;break;}
          }
        }
        const title=clean(post.item_title)||text(e.querySelector('[class^=info-title-text-]'))||text(e.querySelector('.post-title,[class*=title]'));
        const links=Array.from(e.querySelectorAll('a[href]')).map(a=>a.href).filter(url=>/^https:\/\//.test(url)&&/\/(?:explore|discovery\/item|video|share\/post)\//.test(new URL(url).pathname));
        let url=links[0]||'';
        if(p.platform==='douyin'&&post.share_url){
          try{const share=new URL(post.share_url);if(share.protocol==='https:'&&/\/(?:video|share\/(?:video|note))\/\d+\/?$/.test(share.pathname))url=share.origin+share.pathname;}catch{}
        }
        return {title,description:clean(post.desc),text:text(e),id:String(post.objectId||post.aweme_id||e.getAttribute('data-id')||''),created_at:Number(post.createTime||post.create_time||0),native_status:p.platform==='channels'?post.status:undefined,status_text:p.platform==='douyin'?text(e.querySelector('[class^=info-status-]')):text(e).replace(title,''),url};
      });
    }
    case 'select_editor': {
      const e=editor();e.focus();
      const range=document.createRange();range.selectNodeContents(e);
      const selection=getSelection();selection.removeAllRanges();selection.addRange(range);
      return true;
    }
    case 'dismiss_guidance': {
      if(p.platform==='douyin'&&document.body.innerText.includes('Hi，我是发文助手')){
        const button=vis('button[class*=customOkBtn]').find(e=>text(e)==='完成');
        if(button){button.click();return true;}
      }
      return false;
    }
    case 'identity': {
      if (p.platform==='douyin') {
        const response=await fetch('/aweme/v1/creator/user/info/',{credentials:'include'});
        if(!response.ok)throw Error('无法读取抖音账号身份，请重新登录');
        const result=await response.json(),user=result.user_profile||{};
        return {account_id:String(user.unique_id||''),account:clean(user.nick_name||user.nickname)};
      }
      const finder=window._store?.userStore?.finder;
      return {account_id:String(finder?.finderUsername||finder?.uniqId||''),account:clean(finder?.nickname)};
    }
    default:throw Error('未知页面操作');
  }
}
