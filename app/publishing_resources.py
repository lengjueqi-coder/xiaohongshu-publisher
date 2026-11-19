"""Per-account local templates and conservative reads of platform selectors."""
from __future__ import annotations
import json
import re
import sys
import tempfile
import time
from pathlib import Path


def parse_topics(value):
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, list) and all(isinstance(item, str) for item in value):
        values = value
    else:
        raise ValueError('话题必须是文字、粘贴文本或文字列表')
    pieces = []
    for item in values:
        pieces.extend(re.split(r'[#\s,，;；、]+', item.strip()))
    tags = list(dict.fromkeys(
        re.sub(r'[\u200b-\u200f\ufeff]', '', piece).strip()
        for piece in pieces if piece.strip()
    ))
    tags = [tag for tag in tags if tag]
    if not 1 <= len(tags) <= 10:
        raise ValueError('每个话题模板需要 1–10 个话题；超过 10 个请分成多个模板')
    if any(len(tag) > 50 or any(ord(char) < 32 for char in tag) for tag in tags):
        raise ValueError('单个话题不能超过 50 字')
    return tags


def normalize_templates(items):
    if not isinstance(items, list) or len(items) > 50:
        raise ValueError('最多保存 50 个话题模板')
    out = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError('话题模板格式无效')
        name = str(item.get('name') or '').strip()
        if not name or len(name) > 60 or any(x['name'] == name for x in out):
            raise ValueError('模板名称不能为空、重复或超过 60 字')
        out.append({'name':name, 'tags':parse_topics(item.get('tags')), 'source':'local'})
    return out


def resolve_template(payload, local_templates):
    """Local selection freezes exactly its configured topics, never adds filenames."""
    payload = dict(payload)
    name = str(payload.get('local_topic_template') or '')
    if name:
        template = next((x for x in local_templates if x['name'] == name), None)
        if not template:
            raise ValueError('话题模板已删除或属于其它账号，请重新选择')
        payload['tags'] = list(template['tags'])
        payload['topic_template'] = ''
    return payload


def _template_topics(row):
    detail = str(row.get('detail') or '')[:500]
    topics = list(dict.fromkeys(
        re.sub(r'[\u200b-\u200f\ufeff]', '', match).strip()
        for match in re.findall(r'#([^#\s,，;；、]+)', detail) if match.strip()
    ))[:10]
    return topics, detail


_PAGE_READY_JS = r'''(() => {
  const has = (sel) => !!document.querySelector(sel);
  return has('.topicTemplate') || has('.ql-editor') || has('.publish-page')
    || has('.publish-page-content') || has('.collection-plugin');
})()'''

_TEMPLATE_MODAL_VISIBLE_JS = r'''(() => {
  const modal=document.querySelector('.topic-template-list-modal,[class*="topic-template"][class*="modal"]');
  if(!modal)return false;
  const r=modal.getBoundingClientRect();
  return r.width>0&&r.height>0&&getComputedStyle(modal).display!=='none'&&getComputedStyle(modal).visibility!=='hidden';
})()'''

_TEMPLATE_SCROLL_JS = r'''(() => {
  const modal=document.querySelector('.topic-template-list-modal,[class*="topic-template"][class*="modal"]');
  if(!modal)return false;
  const sc=[modal.querySelector('.template-list'),...modal.querySelectorAll('*')]
    .filter(Boolean)
    .filter(e=>e.scrollHeight>e.clientHeight+10)
    .sort((a,b)=>b.scrollHeight-a.scrollHeight)[0];
  if(!sc)return false;
  const before=sc.scrollTop;
  sc.scrollTop=Math.min(sc.scrollHeight,before+Math.max(120,sc.clientHeight*.8));
  return sc.scrollTop>before;
})()'''

_ALBUM_STATE_JS = r'''(() => {
  const clean=s=>(s||'').replace(/\u200b|\ufeff/g,' ').replace(/\s+/g,' ').trim();
  const vis=e=>{if(!e)return false;const r=e.getBoundingClientRect();const s=getComputedStyle(e);
    return r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden';};
  const chrome=/^(创建合集|新建合集|管理合集|搜索|搜索合集|确定|取消|暂无|暂无合集|全部|收起|展开|选择合集|加入合集|添加到合集|添加合集|创建|新建|更多|完成|返回|上一步|编辑|删除|全选|共\d*篇)$/;
  const norm=raw=>clean(raw).replace(/[（(]?\s*共?\s*\d+\s*[篇个条]\s*[)）]?$/,'').trim();
  const collect=root=>{
    const out=new Set();
    for(const row of [...root.querySelectorAll('li,label,[role="option"],[role="radio"],[class*="item"],[class*="option"],[class*="name"]')].filter(vis)){
      const t=norm(row.innerText);
      if(t&&t.length<=40&&!/[，。！？；;]/.test(t)&&!chrome.test(t))out.add(t);
    }
    return out;
  };
  const inputs=[...document.querySelectorAll('input')].filter(vis);
  const search=inputs.find(e=>/搜索|合集|专辑/.test(e.placeholder||''));
  let panel=null,mode='',items=new Set();
  if(search){
    let node=search.parentElement;
    for(let i=0;i<8&&node;i++,node=node.parentElement){
      const got=collect(node);
      if(got.size){panel=node;items=got;mode='search';break;}
    }
  }
  if(!items.size){
    const roots=[...document.querySelectorAll('[role="dialog"],[role="listbox"],[class*="modal"],[class*="dialog"],[class*="popover"],[class*="dropdown"],[class*="drawer"],[class*="collection"],[class*="album"],[class*="select"]')].filter(vis)
      .filter(e=>/合集|专辑/.test(clean(e.innerText)));
    for(const root of roots){
      const got=collect(root);
      if(got.size){panel=root;items=got;mode='popup';break;}
    }
    if(!panel&&search){panel=search.parentElement;mode='search';}
  }
  const text=panel?clean(panel.innerText):'';
  const empty=/暂无|还没有|去创建|新建合集|创建合集/.test(text);
  const found=items.size>0||(mode==='search'&&empty);
  let moved=false;
  if(panel&&panel.scrollHeight>panel.clientHeight+10){
    const before=panel.scrollTop;
    panel.scrollTop=Math.min(panel.scrollHeight,before+Math.max(120,panel.clientHeight*.8));
    moved=panel.scrollTop>before;
  }
  return {found,albums:[...items],moved,mode,sample:text.slice(0,160)};
})()'''

_ALBUM_ENTRY_JS = r'''(() => {
  const clean=s=>(s||'').replace(/\u200b/g,' ').replace(/\s+/g,' ').trim();
  const vis=e=>{if(!e)return false;const r=e.getBoundingClientRect();const s=getComputedStyle(e);
    return r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden';};
  const realClick=el=>{
    el.scrollIntoView({block:'center',inline:'center'});
    for(const t of ['mousedown','mouseup','click']){
      el.dispatchEvent(new MouseEvent(t,{bubbles:true,cancelable:true,view:window}));
    }
  };
  const nodes=[...document.querySelectorAll('button,span,div,a,li,[role="button"]')]
    .filter(vis).filter(e=>!e.closest('[contenteditable="true"]'))
    .filter(e=>{
      const a=e.closest('a[href]');
      if(!a)return true;
      const href=a.getAttribute('href')||'';
      return !href||/^#/.test(href)||/^javascript:/i.test(href)||/publish/i.test(href);
    });
  const strong=['加入合集','添加到合集','选择合集','加入专辑','添加合集','添加专辑'];
  const hit=nodes.find(e=>strong.includes(clean(e.innerText)))
    || nodes.filter(e=>{
         const t=clean(e.innerText);
         return t&&t.length<15&&/合集|专辑/.test(t)&&!/管理|创建|新建/.test(t);
       }).sort((a,b)=>clean(a.innerText).length-clean(b.innerText).length)[0];
  if(!hit)return false;
  realClick(hit.closest('button,a,[role="button"],li')||hit);
  return true;
})()'''

_VISIBLE_TEXTS_JS = r'''(() => {
  const clean=s=>(s||'').replace(/\u200b|\ufeff/g,' ').replace(/\s+/g,' ').trim();
  const vis=e=>{if(!e)return false;const r=e.getBoundingClientRect();const s=getComputedStyle(e);
    return r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden';};
  const out=[];
  for(const e of document.querySelectorAll('li,label,span,div,p,a,button')){
    if(!vis(e))continue;
    const t=clean(e.innerText);
    if(t&&t.length<=40)out.push(t);
    if(out.length>4000)break;
  }
  return out;
})()'''

_PUBLISH_STATE_JS = r'''(() => {
  const images=document.querySelectorAll(".img-preview-area .pr,.img-preview-area .img-preview-item").length;
  const title=document.querySelector('div.d-input input');
  const editor=document.querySelector("div.ql-editor, div.tiptap.ProseMirror, [contenteditable='true']");
  return {
    images,
    title:(title&&title.value||'').trim(),
    body:(editor&&editor.innerText||'').trim(),
    url:location.href,
  };
})()'''

_RESET_PUBLISH_JS = r'''(() => {
  const clean=s=>(s||'').replace(/\s+/g,'').trim();
  const vis=e=>{if(!e)return false;const r=e.getBoundingClientRect();const s=getComputedStyle(e);
    return r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden';};
  const realClick=el=>{
    el.scrollIntoView({block:'center',inline:'center'});
    for(const t of ['mousedown','mouseup','click']){
      el.dispatchEvent(new MouseEvent(t,{bubbles:true,cancelable:true,view:window}));
    }
  };
  const cands=[...document.querySelectorAll('a,button,li,[role="menuitem"],div,span')]
    .filter(vis).filter(e=>!e.closest('[contenteditable="true"]'))
    .filter(e=>clean(e.innerText)==='发布笔记');
  if(!cands.length)return false;
  const link=cands.find(e=>e.tagName==='A'&&/publish/i.test(e.getAttribute('href')||''));
  const leaf=cands.find(e=>!cands.some(o=>o!==e&&e.contains(o)))||cands[0];
  realClick((link||leaf).closest('a,button,li,[role="menuitem"]')||link||leaf);
  return true;
})()'''


def _album_candidates(before, after):
    """打开弹层前后对比：新出现的短文本当作合集名兜底。"""
    skip = re.compile(
        r'^(创建合集|新建合集|管理合集|搜索|搜索合集|确定|取消|暂无|暂无合集|全部|收起|展开'
        r'|选择合集|加入合集|添加到合集|添加合集|创建|新建|更多|完成|返回|上一步|全选'
        r'|发布|发布笔记|存草稿|定时发布|上传图文|上传视频|公开可见|仅自己可见|原创|地点|话题'
        r'|添加话题|添加地点|添加合集|设置|账号|退出|通知|帮助|下载)$'
    )
    junk = re.compile(r'发布|上传|账号|设置|下载|通知|帮助|退出|头像|登录|注册')
    seen = set(before or [])
    out = []
    for item in after or []:
        text = str(item or '').strip()
        if not text or text in seen or len(text) > 30:
            continue
        if skip.match(text) or junk.search(text):
            continue
        if re.fullmatch(r'[\d\s.,:：%+\-]+', text):
            continue
        text = re.sub(r'\s*[（(]?\s*共?\s*\d+\s*[篇个条]\s*[)）]?$', '', text).strip()
        if not text or re.search(r'[，。！？；;]', text) or skip.match(text):
            continue
        out.append(text)
    return list(dict.fromkeys(out))


def _close_template_modal(page):
    """Close the topic-template modal. Newer UI keeps its portal mounted briefly."""
    for _ in range(4):
        if not page.evaluate(_TEMPLATE_MODAL_VISIBLE_JS):
            return True
        page.evaluate(r'''(() => {
          const modal=document.querySelector('.topic-template-list-modal,[class*="topic-template"][class*="modal"]');
          if(!modal)return;
          const close=modal.querySelector('.d-modal-close,[aria-label*="关闭"],button[class*="close"]');
          if(close){close.click();return;}
          const mask=document.querySelector('.d-modal-mask,[class*="mask"]');
          if(mask)mask.click();
        })()''')
        time.sleep(.25)
        if not page.evaluate(_TEMPLATE_MODAL_VISIBLE_JS):
            return True
        page.press_key('Escape')
        time.sleep(.25)
    return not page.evaluate(_TEMPLATE_MODAL_VISIBLE_JS)


def _placeholder_image():
    """生成一张纯色占位图，把发布页撑开让合集入口出现，读完由「发布笔记」重置清除。"""
    path = Path(tempfile.gettempdir()) / "xhs-resource-read-placeholder.jpg"
    try:
        from PIL import Image
        Image.new("RGB", (1080, 1080), (244, 244, 244)).save(path, quality=88)
        return str(path)
    except Exception:
        return ""


def _open_temp_publish_page(page):
    """当前标签不在发布页时，临时打开发布页并放一张占位图。

    小红书的合集弹层和话题模板入口只在发布页渲染，而且通常要页面上有图片
    才会出现“加入合集”，所以需要这样把页面撑开；不会点击发布。
    """
    from xhs.publish import _click_publish_tab, _navigate_to_publish_page, _wait_for_upload_complete
    _navigate_to_publish_page(page)
    try:
        _click_publish_tab(page, "上传图文")
    except Exception:
        pass
    deadline = time.time() + 20
    while time.time() < deadline:
        if page.evaluate(_PAGE_READY_JS):
            break
        time.sleep(.4)
    placeholder = _placeholder_image()
    if not placeholder:
        raise ValueError("无法生成占位图片，请稍后重试同步")
    page.set_file_input(".upload-input", [placeholder])
    _wait_for_upload_complete(page, 1)
    return placeholder


def _publish_image_count(page):
    """返回发布页已载入的图片数量；页面正在跳转时返回 -1。"""
    try:
        return int(page.evaluate(
            r'''(() => document.querySelectorAll(".img-preview-area .pr,.img-preview-area .img-preview-item").length)()'''
        ) or 0)
    except Exception:
        return -1


def _restore_page(page, url, opened_temp):
    """读完资源后用「发布笔记」把临时发布页重置干净。

    小红书不允许删除唯一一张图片（会提示“发布笔记需要至少保留1张照片”），
    所以按平台自己的做法：点左侧「发布笔记」重新进入，页面重置后占位图就没了。
    """
    if not opened_temp:
        return
    from xhs.urls import PUBLISH_URL
    from xhs.publish import _click_publish_tab
    reset = False
    try:
        reset = bool(page.evaluate(_RESET_PUBLISH_JS))
    except Exception:
        reset = False
    if reset:
        time.sleep(1.0)
        try:
            # 「发布笔记」带下拉箭头时只弹出菜单，这里再点一下“上传图文”完成跳转。
            _click_publish_tab(page, "上传图文")
        except Exception:
            pass
        time.sleep(1.8)
    remaining = _publish_image_count(page)
    if not reset or remaining != 0:
        try:
            page.evaluate(f"(() => {{ location.href = {json.dumps(PUBLISH_URL)}; return true; }})()")
        except Exception:
            try:
                page.navigate(PUBLISH_URL)
            except Exception:
                pass
        deadline = time.time() + 20
        while time.time() < deadline:
            time.sleep(.8)
            if _publish_image_count(page) == 0:
                break
    try:
        if url.startswith("http") and "xiaohongshu.com" in url and "/publish" not in url:
            page.navigate(url)
    except Exception:
        pass


def read_resources(skill_dir):
    scripts = str(skill_dir/'scripts')
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    from xhs.bridge import BridgePage
    from xhs.publish import _click_topic_template_entry, _template_state
    page = BridgePage()
    url = page.evaluate('location.href')
    opened_temp = False
    if 'creator.xiaohongshu.com' not in url or not page.evaluate(_PAGE_READY_JS):
        try:
            _open_temp_publish_page(page)
            opened_temp = True
        except Exception as exc:
            raise ValueError(
                f'打开临时发布页读取合集和话题模板失败：{exc}。请确认 Chrome 已打开且已登录小红书，然后再试。'
            ) from exc
    else:
        # 已经在发布页：合集入口要页面上有图才会出现。
        # 空页面补一张占位图；只有一张图且没有标题正文时，视为上次没清掉的占位图，
        # 读完同样用「发布笔记」重置掉。
        try:
            state = page.evaluate(_PUBLISH_STATE_JS) or {}
        except Exception:
            state = {}
        empty_draft = not state.get('title') and not state.get('body')
        images = int(state.get('images') or 0)
        if empty_draft and images <= 1:
            if not images:
                try:
                    from xhs.publish import _wait_for_upload_complete
                    placeholder = _placeholder_image()
                    if placeholder:
                        page.set_file_input(".upload-input", [placeholder])
                        _wait_for_upload_complete(page, 1)
                except Exception:
                    pass
            opened_temp = True

    result = {
        'templates': [],
        'albums': [],
        'status': {
            'templates': {'ok': False, 'count': 0, 'topics_complete': False, 'message': ''},
            'albums': {'ok': False, 'count': 0, 'message': ''},
        },
        'errors': [],
    }
    already_open = bool(page.evaluate(_TEMPLATE_MODAL_VISIBLE_JS))
    try:
        if not already_open and not _click_topic_template_entry(page):
            raise ValueError('未找到平台话题模板入口')
        # The platform renders template cards after the modal opens; wait until
        # the card count stops growing so all six templates are captured.
        deadline = time.time() + 6
        last_count = -1
        stable = 0
        while time.time() < deadline:
            state = _template_state(page)
            if state.get('found'):
                count = len({row['name'] for row in state.get('rows', []) if row.get('name')})
                stable = stable + 1 if count and count == last_count else 0
                last_count = count
                if stable >= 2:
                    break
            time.sleep(.25)
        seen = {}
        for _ in range(30):
            state = _template_state(page)
            if not state.get('found'):
                raise ValueError('未读到平台话题模板弹窗')
            for row in state.get('rows', []):
                if row.get('name'):
                    topics, detail = _template_topics(row)
                    seen[row['name']] = {
                        'name': row['name'], 'topics': topics, 'topics_confirmed': bool(topics),
                        'detail': detail, 'source': 'platform',
                    }
            if not page.evaluate(_TEMPLATE_SCROLL_JS):
                break
            time.sleep(.2)
        if not seen:
            raise ValueError('平台话题模板弹窗已打开，但没有读到模板卡片')
        result['templates'] = list(seen.values())
        missing = sum(not item['topics_confirmed'] for item in result['templates'])
        result['status']['templates'] = {
            'ok': True,
            'count': len(result['templates']),
            'topics_complete': missing == 0,
            'message': (
                f'已读取 {len(result["templates"])} 个平台模板及完整话题'
                if missing == 0 else
                f'已读取 {len(result["templates"])} 个平台模板名称；{missing} 个模板未能确认完整话题'
            ),
        }
    except Exception as exc:
        message = str(exc)
        result['status']['templates']['message'] = message
        result['errors'].append(message)
    finally:
        if already_open:
            closed = True
        else:
            try:
                closed = _close_template_modal(page)
            except Exception:
                closed = False
        if not closed:
            message = '话题模板弹窗未能自动关闭；请手动点右上角关闭。合集仍会继续读取。'
            result['errors'].append(message)
    # The album popover is a portal. Read it by its current DOM even if the
    # template modal above could not be dismissed.
    try:
        page.press_key('Escape')
        time.sleep(.3)
    except Exception:
        pass
    before_texts = []
    try:
        before_texts = page.evaluate(_VISIBLE_TEXTS_JS) or []
    except Exception:
        before_texts = []
    opened = page.evaluate(_ALBUM_ENTRY_JS)
    if opened:
        try:
            seen_albums = []
            found = False
            sample = ''
            mode = ''
            time.sleep(1.1)
            for _ in range(24):
                state = page.evaluate(_ALBUM_STATE_JS) or {}
                sample = state.get('sample') or sample
                mode = state.get('mode') or mode
                if state.get('found'):
                    found = True
                    seen_albums.extend(state.get('albums') or [])
                    if not state.get('moved'):
                        break
                elif not found:
                    time.sleep(.25)
                    continue
                else:
                    break
                time.sleep(.2)
            if not found:
                # 兜底：对比弹层打开前后新出现的短文本，认出合集名。
                try:
                    after_texts = page.evaluate(_VISIBLE_TEXTS_JS) or []
                except Exception:
                    after_texts = []
                guessed = _album_candidates(before_texts, after_texts)
                if guessed:
                    seen_albums = guessed
                    found = True
                    mode = 'diff'
            if not found:
                detail = f'（页面文字：{sample[:60]}）' if sample else ''
                raise ValueError(f'未读到可确认的合集弹窗{detail}')
            result['albums'] = list(dict.fromkeys(seen_albums))
            if not result['albums']:
                message = '合集列表读取成功，当前账号没有可用合集'
            elif mode == 'diff':
                message = f'已识别 {len(result["albums"])} 个合集，请核对名称'
            else:
                message = f'已读取 {len(result["albums"])} 个合集'
            result['status']['albums'] = {
                'ok': True,
                'count': len(result['albums']),
                'message': message,
            }
        except Exception as exc:
            message = str(exc)
            result['status']['albums']['message'] = message
            result['errors'].append(message)
        finally:
            page.press_key('Escape')
    else:
        message = '未找到合集入口；保留上次成功缓存，可手动输入合集名称'
        result['status']['albums']['message'] = message
        result['errors'].append(message)
    _restore_page(page, url, opened_temp)
    result['status']['temp_page'] = opened_temp
    return result
