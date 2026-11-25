"""Read back the creator form; fail closed for unattended submission."""
from __future__ import annotations

import json
import re
import sys
from datetime import date
from pathlib import Path
import time


def page_for(skill_dir, scope="publish", expected_tab=None):
    scripts = str(skill_dir / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    from xhs.bridge import BridgePage
    return BridgePage(scope=scope, create=scope != "publish", expected_tab=expected_tab)


def add_mentions(page, mentions):
    for value in mentions:
        name, sep, uid = value.partition("|")
        if not sep or not name or not re.fullmatch(r"[a-zA-Z0-9_-]{6,128}", uid):
            raise ValueError("@ 用户请按每行“昵称|小红书用户ID”填写，避免选错同名账号")
        page.evaluate("""(() => {const e=document.querySelector('.ql-editor,.tiptap.ProseMirror');if(!e)throw Error('未找到正文');
            e.focus();const r=document.createRange();r.selectNodeContents(e);r.collapse(false);
            const s=getSelection();s.removeAllRanges();s.addRange(r);document.execCommand('insertText',false,' ');})()""")
        page.type_text("@" + name, delay_ms=80)
        found = False
        for _ in range(12):
            found = page.evaluate("""(() => {const uid=UID;const visible=e=>e.getBoundingClientRect().width>0;
              const rows=Array.from(document.querySelectorAll('[data-user-id],a[href*="/user/profile/"]')).filter(visible)
                .filter(e=>!e.closest('.ql-editor,.tiptap.ProseMirror')).filter(e=>e.getAttribute('data-user-id')===uid || (e.getAttribute('href')||'').split('?')[0].endsWith('/'+uid));
              if(rows.length!==1)return false;rows[0].click();return true;})()""".replace("UID", json.dumps(uid)))
            if found:
                break
            time.sleep(0.5)
        if not found:
            raise ValueError(f"没有找到可确认用户 ID 的 @{name}，已暂停，避免选错账号")


FORM_STATE = r"""(() => {
  const visible=e=>!!e && e.getBoundingClientRect().width>0 && getComputedStyle(e).visibility!=='hidden';
  const text=e=>(e?.innerText||e?.textContent||'').trim();
  const card=Array.from(document.querySelectorAll('.custom-switch-card')).find(e=>/原创声明/.test(text(e)));
  const isOn=e=>e ? (e.querySelector('input[type="checkbox"]')?.checked ??
    (e.getAttribute('aria-checked')==='true' || /(?:^|\s)(?:checked|active|is-checked|d-switch-checked)(?:\s|$)/.test(e.className))) : null;
  // Try both Quill (.ql-editor) and Tiptap/ProseMirror editors — creator site migrated.
  const editor = document.querySelector('.ql-editor, [contenteditable="true"].ProseMirror, .tiptap.ProseMirror, div.tiptap')
    || document.querySelector('.ql-editor, [contenteditable="true"]');
  const choice=(selector, emptyLabels)=>{
    const controls=[...document.querySelectorAll(selector)].filter(visible);
    if(controls.length!==1)return null;
    const control=controls[0];
    const selected=control.querySelector('.d-select-content,[class*="selected-name"]');
    const lines=text(selected||control).split(/\n/).map(v=>v.trim()).filter(Boolean);
    const values=lines.filter(v=>!emptyLabels.includes(v));
    return values.length===1?values[0]:values.length===0?'':null;
  };
  const album=()=>{
    const labels=['加入合集','添加到合集','添加合集','选择合集','合集','不选择合集'];
    const primary=choice('.collection-plugin',labels);
    if(primary!==null)return primary;
    const candidates=[...document.querySelectorAll('input[placeholder="选择合集"],[role="combobox"],.d-select-content,[class*="placeholder"],[class*="select"]')]
      .filter(e=>visible(e)&&!e.closest('[contenteditable="true"],[role="dialog"],[role="listbox"]'))
      .filter(e=>e.placeholder==='选择合集'||text(e)==='选择合集')
      .filter(e=>e.tagName!=='INPUT'||!e.value);
    const leaves=candidates.filter(e=>!candidates.some(child=>child!==e&&e.contains(child)));
    return leaves.length===1?'':null;
  };
  const alerts=Array.from(document.querySelectorAll('[role="alert"],.d-toast,.d-message,.semi-toast')).filter(visible).map(text);
  const riskElements=Array.from(document.querySelectorAll('iframe[src*="captcha"],[class*="captcha"],[class*="verify-dialog"],.login-container,.login-box')).filter(visible);
  const topicSelector='.tiptap-topic,.ql-topic,[data-topic-id]';
  const topicName=e=>text(e).replace(/[\u200b\ufeff]/g,'').replace(/^#\s*/,'').replace(/\s*\[话题\]#?$/,'').trim();
  const inline=node=>{
    if(node.nodeType===Node.TEXT_NODE)return node.nodeValue;
    if(node.nodeType!==Node.ELEMENT_NODE)return '';
    if(node.matches(topicSelector))return '#'+topicName(node);
    if(node.matches('.ProseMirror-trailingBreak,[aria-hidden="true"]'))return '';
    if(node.tagName==='BR')return '\n';
    return Array.from(node.childNodes,inline).join('');
  };
  // innerText inserts CSS paragraph margins as extra blank lines. Quill and
  // Tiptap store each logical paragraph, including an empty one, as one block.
  const blocks=editor?[...editor.children]:[];
  const editorText=editor&&blocks.length&&blocks.every(e=>e.matches('p,div'))&&
    [...editor.childNodes].every(n=>n.nodeType!==Node.TEXT_NODE||!n.nodeValue.trim())
    ? blocks.map(e=>e.childNodes.length===1&&e.firstChild.nodeName==='BR'?'':inline(e)).join('\n')
    : (editor?inline(editor):'');
  return {url:location.href,timezone:Intl.DateTimeFormat().resolvedOptions().timeZone,title:document.querySelector('div.d-input input')?.value||'',content:editorText,
    image_count:Array.from(document.querySelectorAll('.img-preview-area .pr')).filter(visible).length,
    native_image_refs:IMAGE_PREVIEWS,
    album:album(),
    location:choice('.address-card-select',['添加地点','选择地点','不显示位置']),
    video:VIDEO_PREVIEW,
    visibility:text(document.querySelector('div.permission-card-wrapper div.d-select-content')),
    schedule:document.querySelector('.date-picker-container input')?.value||'',
    schedule_on:isOn(document.querySelector('.post-time-wrapper .d-switch')),
    original:isOn(card?.querySelector('.d-switch')),alerts,
    allow_copy:isOn(Array.from(document.querySelectorAll('.custom-switch-card')).find(e=>text(e)==='允许正文复制')?.querySelector('.d-switch')),
    source_note:text(document.querySelector('.publish-page-content-setting-content .wrapper .notation')),
    source_dialog:[...document.querySelectorAll('.self-shooting-contain')].some(visible),
    declaration:text(document.querySelector('.publish-page-content-setting-content .wrapper .d-select-content')),
    activities:Array.from(document.querySelectorAll('.activity-card')).filter(e=>visible(e)&&text(e.querySelector('.activity-action'))==='取消关联').map(e=>text(e.querySelector('.activity-name'))),
    topics:Array.from(editor?.querySelectorAll(topicSelector)||[]).filter(e=>!e.parentElement?.closest(topicSelector)).map(topicName),
    risky:riskElements.length>0 || alerts.some(t=>/验证码|安全验证|操作频繁|访问频繁|账号异常|限制发布|请先登录/.test(t)),
    mention_ids:Array.from(editor?.querySelectorAll('[data-user-id],[data-id],a[href*="/user/profile/"]')||[]).map(n=>n.getAttribute('data-user-id')||n.getAttribute('data-id')||(n.getAttribute('href')||'').match(/\/user\/profile\/([^?/#]+)/)?.[1]||'')};
})()""".replace("VIDEO_PREVIEW", (Path(__file__).parent / "vendor/xiaohongshu-skills/scripts/xhs/video_preview.js").read_text(encoding="utf-8").strip()).replace("IMAGE_PREVIEWS", (
    Path(__file__).parent / "vendor/xiaohongshu-skills/scripts/xhs/image_previews.js"
).read_text(encoding="utf-8").strip())


def validate_state(state, payload):
    """Check the official form against the reviewed task before submission."""
    if not isinstance(state, dict) or state.get("risky"):
        raise ValueError("出现登录、安全验证或频率限制，请在 Chrome 中处理后恢复")
    def cleaned(value):
        return str(value).replace("\r\n", "\n").replace("\r", "\n").replace("\u00a0", " ").replace("\u200b", "").strip()
    expected_title = str(payload.get("title") or "").strip()
    page_title = str(state.get("title") or "").strip()
    if expected_title and expected_title != page_title:
        raise ValueError("发布页标题与任务不一致")
    expected_content = cleaned(payload.get("content") or "")
    page_content = cleaned(state.get("content") or "")
    if not expected_content or not page_content.startswith(expected_content):
        raise ValueError("发布页正文与任务不一致")
    remainder = page_content[len(expected_content):].strip()
    for tag in payload.get("tags", []):
        name = cleaned(str(tag).lstrip("#"))
        for token in (f"#{name}[话题]#", f"#{name}"):
            if remainder.startswith(token):
                remainder = remainder[len(token):].lstrip()
                break
    for mention in payload.get("mentions", []):
        token = "@" + cleaned(mention.partition("|")[0])
        if remainder.startswith(token):
            remainder = remainder[len(token):].lstrip()
    if remainder:
        raise ValueError("发布页正文混入任务之外的内容")
    expected_images = len(payload.get("images") or [])
    if payload.get('media_type')=='video':
        video=state.get('video') or {}
        if not video.get('src') or not video.get('duration'):raise ValueError('视频尚未上传、转码或预览未就绪')
        if payload.get('native_video_src') and payload['native_video_src']!=video['src']:raise ValueError('官方视频在核对后发生变化')
    if expected_images and int(state.get("image_count") or 0) != expected_images:
        raise ValueError("发布页图片数量不一致或上传未完成")
    if expected_images and "native_image_refs" in payload:
        refs = payload["native_image_refs"]
        if not isinstance(refs, list) or len(refs) != expected_images or not all(refs) or refs != state.get("native_image_refs"):
            raise ValueError("小红书图片已被替换、调序或缺少上传凭证，请重新填写")
    visibility = payload.get("visibility") or "公开可见"
    if visibility not in state.get("visibility", ""):
        raise ValueError(f"无法确认可见范围为“{visibility}”，已暂停")
    if state.get("original") is None:
        raise ValueError("无法确认原创声明开关状态")
    if state["original"] is not bool(payload.get("original", True)):
        raise ValueError("发布页原创声明与任务不一致")
    for key,label in [('allow_copy','允许正文复制'),('activities','关联活动'),('declaration','内容类型声明')]:
        if key not in payload:continue
        expected=payload[key]
        actual=state.get(key)
        if key=='allow_copy' and payload.get('media_type')=='video' and actual is None and expected is True:continue
        if key=='declaration' and state.get('source_dialog'):
            raise ValueError('小红书来源声明尚未确认')
        if key=='declaration' and str(expected).startswith('内容来源声明：'):
            expected = expected.split('：', 1)[1]
            if state.get('source_note') != source_note(payload):
                raise ValueError('小红书来源声明详细信息与任务不一致')
        if key=='declaration' and not expected:expected='添加内容类型声明' if actual else ''
        if key=='activities':expected,actual=sorted(expected),sorted(actual or [])
        if expected!=actual:raise ValueError(f'小红书{label}不一致：期望 {expected}，页面 {actual}')
    if 'topics' in state and state['topics']!=[t.lstrip('#') for t in payload.get('tags',[])]:
        raise ValueError('小红书话题未绑定到对应的原生话题节点')
    scheduled = payload.get("schedule_time", "")
    if scheduled:
        if payload.get("timezone") and state.get("timezone") != payload["timezone"]:
            raise ValueError("Chrome 时区与计划时区不同，请调整计划时区并重新排期，避免发布错时")
        actual = state.get("schedule", "").replace(" ", "T")[:16]
        if actual != scheduled.replace(" ", "T")[:16] or state.get("schedule_on") is not True:
            raise ValueError("发布页没有保留任务要求的定时时间")
    elif state.get("schedule_on"):
        raise ValueError("立即发布任务的定时开关仍开启")
    for key, label in (("album", "合集"), ("location", "地点")):
        value = payload.get(key)
        if key in payload and (value or "") != state.get(key):
            raise ValueError(f"小红书{label}与任务不一致或无法读取，请在平台核对")
    for tag in payload.get("tags", []):
        if cleaned(str(tag).lstrip("#")) not in page_content:
            raise ValueError(f"话题 #{tag} 未出现在发布正文，已暂停")
    for value in payload.get("mentions", []):
        uid = value.partition("|")[2]
        if not uid or uid not in state.get("mention_ids", []):
            raise ValueError("无法确认 @ 用户已绑定到正确的用户 ID")
    return state


def verify(skill_dir, payload, apply_mentions=False):
    page = page_for(skill_dir, expected_tab=payload.get("work_tab_id"))
    if apply_mentions and payload.get("mentions"):
        current=page.evaluate(FORM_STATE)
        present=set((current or {}).get('mention_ids',[]))
        missing=[value for value in payload["mentions"] if value.partition('|')[2] not in present]
        add_mentions(page, missing)
    return validate_state(page.evaluate(FORM_STATE), payload)




def source_note(payload):
    if payload.get('declaration') == '内容来源声明：自主拍摄':
        day = str(payload.get('shooting_date') or '')
        location = str(payload.get('shooting_location') or '').strip()
        if not location or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', day):
            raise ValueError('自主拍摄需要拍摄日期和完整地点')
        date.fromisoformat(day)
        return f'拍摄于{day}，{location}'
    source = str(payload.get('repost_source') or '').strip()
    if not source:
        raise ValueError('来源转载需要填写媒体名称')
    return '信息转载于' + source


def apply_extra_settings(skill_dir, payload):
    if not any(key in payload for key in ('declaration','activities','allow_copy')):return
    page=page_for(skill_dir, expected_tab=payload.get("work_tab_id"))
    state=page.evaluate(FORM_STATE)
    if 'allow_copy' in payload and state['allow_copy'] is not None and state['allow_copy']!=payload['allow_copy']:
        page.evaluate("""[...document.querySelectorAll('.custom-switch-card')].find(e=>e.innerText.trim()==='允许正文复制').querySelector('.d-switch').click()""")
    if 'activities' in payload:
        wanted=set(payload['activities'])
        for name in sorted(set(state['activities']) - wanted)+sorted(wanted - set(state['activities'])):
            page.evaluate("""(()=>{const cards=[...document.querySelectorAll('.activity-card')].filter(e=>e.offsetWidth&&e.querySelector('.activity-name')?.innerText.trim()===NAME);
                if(cards.length!==1)throw Error('官方页面没有唯一匹配的活动：'+NAME);
                cards[0].querySelector('.activity-action').click();})()""".replace('NAME',json.dumps(name)))
            time.sleep(.25)
    label=payload.get('declaration')
    if label == '' and state['declaration'] not in ('', '添加内容类型声明'):
        for attempt in range(3):
            page.evaluate("""(() => {
                const e=document.querySelector('.publish-page-content-setting-content .wrapper .d-select');
                e.scrollIntoView({block:'center',behavior:'instant'});
                e.dispatchEvent(new MouseEvent('mouseenter',{bubbles:true}));
                e.dispatchEvent(new MouseEvent('mouseover',{bubbles:true}));
            })()""")
            time.sleep(.35)
            point = page.evaluate("""(() => {
                const e=document.querySelector('.publish-page-content-setting-content .wrapper .d-select-suffix-clear');
                if(!e)return null;
                const r=e.getBoundingClientRect(), x=r.x+r.width/2, y=r.y+r.height/2;
                return e.contains(document.elementFromPoint(x,y)) ? {x,y} : null;
            })()""")
            if point:
                page.trusted_click_at(point['x'], point['y'])
            time.sleep(.25)
            if page.evaluate(FORM_STATE)['declaration'] in ('', '添加内容类型声明'):
                break
        else:
            raise ValueError('未能清除小红书内容声明，请在官方页面核对')
    nested = bool(label and label.startswith('内容来源声明：'))
    expected_note = source_note(payload) if nested else ''
    if label and (label.split('：')[-1] != state['declaration'] or (nested and state.get('source_note') != expected_note)):
        page.evaluate("""(()=>{const e=document.querySelector('.publish-page-content-setting-content .wrapper [tabindex]');
            e.blur();e.scrollIntoView({block:'center',behavior:'instant'});e.focus();})()""")
        for part in label.split('：'):
            for attempt in range(12):
                time.sleep(.25)
                clicked=page.evaluate("""(label=>{
                    const items=[...document.querySelectorAll('.d-option-name')].filter(e=>e.offsetWidth&&e.innerText.trim()===label);
                    if(items.length!==1)return false;
                    const option=items[0].closest('.d-option')||items[0];
                    if(label==='内容来源声明'){
                        option.dispatchEvent(new MouseEvent('mouseover',{bubbles:true}));
                        option.dispatchEvent(new MouseEvent('mouseenter',{bubbles:true}));
                    } else option.click();
                    return true;
                })("""+json.dumps(part)+")")
                if clicked:break
                if attempt==4 and part==label.split('：')[0]:
                    page.click_element('.publish-page-content-setting-content .wrapper .d-select')
            if not clicked:raise ValueError('请在小红书后台工作页手动确认声明选项：'+part)
        if nested:
            script = (Path(__file__).parent / 'static' / 'xhs-source.js').read_text()
            data = {'repost': label.endswith('来源转载'), 'source': payload.get('repost_source', '').strip(),
                    'location': payload.get('shooting_location', '').strip(), 'date': payload.get('shooting_date', '')}
            result = page.evaluate('(' + script + ')(' + json.dumps(data) + ').catch(e => ({error:e.message}))')
            if result is not True:
                raise ValueError((result or {}).get('error', '未能确认小红书来源声明'))


def read_management(skill_dir, published_only=False):
    page = page_for(skill_dir, "management")
    page.navigate("https://creator.xiaohongshu.com/new/note-manager")
    page.wait_for_load(timeout=30)
    if published_only:
        for _ in range(20):
            selected=page.evaluate("""(() => {const e=[...document.querySelectorAll('.tab-item')].find(e=>e.innerText.trim()==='已发布');if(!e)return false;e.click();return true;})()""")
            if selected:break
            time.sleep(.25)
        if not selected:raise ValueError('找不到官方“已发布”筛选，未推断发布结果')
        time.sleep(1)
    for _ in range(8):
        cards = page.evaluate(r"""(() => {
          const cards=Array.from(document.querySelectorAll('.note-card'));
          const rows=cards.map(e=>{
            const text=(e.innerText||'').trim();
            const title=(e.querySelector('.note-card__title')?.innerText||'').trim();
            const statusLines=text.split('\n').filter(line=>line.trim()!==title).map(line=>line.trim());
            let noteId='';
            try {
              const raw=JSON.parse(e.getAttribute('data-impression')||'{}');
              noteId=String(raw?.noteTarget?.value?.noteId||raw?.noteId||'');
            } catch (_) {}
            return {
              text,
              title,
              time:(e.querySelector('.note-card__time')?.innerText||'').trim(),
              note_id:noteId,
              private:statusLines.includes('仅自己可见'),
              scheduled:statusLines.includes('定时发布'),
              published_context:document.querySelector('.tab-item--active')?.innerText.trim()==='已发布',
              url:e.querySelector('a[href*="/explore/"],a[href*="/discovery/item/"]')?.href||''
            };
          });
          const key=row=>row.note_id||row.text;
          return [...new Map(rows.map(row=>[key(row),row])).values()];
        })()""")
        if cards:
            return cards
        time.sleep(1)
    raise ValueError("笔记管理页面未读取到结果，可能需要重新登录或页面已改版")


def match_receipt(cards, payload):
    from datetime import datetime
    schedule_time = str(payload.get("schedule_time") or "").strip()
    reference_time = str(payload.get("receipt_reference_time") or "").strip()
    expected = datetime.fromisoformat(schedule_time or reference_time) if (schedule_time or reference_time) else None
    tolerance = 300
    matches = []
    for card in cards:
        lines = [line.strip() for line in card.get("text", "").splitlines() if line.strip()]
        title = str(card.get("title") or "").strip()
        if not title:
            title = next((line for line in lines if line == payload["title"]), "")
        if title != payload["title"]:
            continue
        time_lines = [str(card["time"]).strip()] if card.get("time") else [line for line in lines if line!=title]
        dates = [re.search(r"20\d{2}-\d{2}-\d{2}[ T]\d{2}:\d{2}", line) for line in time_lines]
        dates = [datetime.fromisoformat(m[0]) for m in dates if m]
        if expected is None or any(abs((dt-expected).total_seconds()) <= tolerance for dt in dates):
            matches.append(card)
    if len(matches) != 1:
        return {"status": "unconfirmed", "reason": "未找到唯一匹配标题和时间的笔记"}
    card = matches[0]
    status_lines = [line.strip() for line in card["text"].splitlines() if line.strip() != payload['title']]
    text = "\n".join(status_lines)
    is_private = card.get("private") is True or "仅自己可见" in status_lines
    if payload.get("visibility") == "仅自己可见" and not is_private:
        return {"status": "unconfirmed", "reason": "匹配到标题和时间，但管理页没有显示“仅自己可见”"}
    if re.search(r"审核不通过|发布失败|违规|不予通过|已删除", text):
        return {"status": "rejected", "reason": text[:300]}
    if re.search(r"审核中|待审核", text):
        return {"status": "reviewing", "note_id": card.get("note_id", ""), "url": card.get("url", ""), "reason": "平台仍在审核"}
    if card.get("scheduled") is True or re.search(r"定时发布|待发布", text):
        return {"status": "scheduled", "note_id": card.get("note_id", ""), "url": card.get("url", ""), "reason": "平台仍显示待发布"}
    if re.search(r"已发布|发布成功", text) or all(word in text for word in ("浏览", "点赞")) or (card.get('published_context') is True and card.get('note_id')):
        return {"status": "published", "note_id": card.get("note_id", ""), "url": card.get("url", ""), "reason": "管理页匹配到已发布笔记"}
    return {"status": "unconfirmed", "reason": "找到匹配笔记，但无法确认平台最终状态"}
