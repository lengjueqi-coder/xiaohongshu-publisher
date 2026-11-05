"""Read back the creator form; fail closed for unattended submission."""
from __future__ import annotations

import json
import re
import sys
import time


def page_for(skill_dir):
    scripts = str(skill_dir / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    from xhs.bridge import BridgePage
    return BridgePage()


def add_mentions(page, mentions):
    for value in mentions:
        name, sep, uid = value.partition("|")
        if not sep or not name or not re.fullmatch(r"[a-zA-Z0-9_-]{6,128}", uid):
            raise ValueError("@ 用户请按每行“昵称|小红书用户ID”填写，避免选错同名账号")
        page.evaluate("""(() => {const e=document.querySelector('.ql-editor');if(!e)throw Error('未找到正文');
            e.focus();const r=document.createRange();r.selectNodeContents(e);r.collapse(false);
            const s=getSelection();s.removeAllRanges();s.addRange(r);document.execCommand('insertText',false,' ');})()""")
        page.type_text("@" + name, delay_ms=80)
        found = False
        for _ in range(12):
            found = page.evaluate("""(() => {const uid=UID;const visible=e=>e.getBoundingClientRect().width>0;
              const rows=Array.from(document.querySelectorAll('[data-user-id],a[href*="/user/profile/"]')).filter(visible)
                .filter(e=>!e.closest('.ql-editor')).filter(e=>e.getAttribute('data-user-id')===uid || (e.getAttribute('href')||'').split('?')[0].endsWith('/'+uid));
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
  const e=document.querySelector('.ql-editor');
  const rows=Array.from(document.querySelectorAll('.custom-switch-card,.permission-card-wrapper,.post-time-wrapper,.publish-config-item,.d-form-item,[class*="album"],[class*="collection"],[class*="location"]')).filter(visible);
  const alerts=Array.from(document.querySelectorAll('[role="alert"],.d-toast,.d-message,.semi-toast')).filter(visible).map(text);
  const riskElements=Array.from(document.querySelectorAll('iframe[src*="captcha"],[class*="captcha"],[class*="verify-dialog"],.login-container,.login-box')).filter(visible);
  return {url:location.href,timezone:Intl.DateTimeFormat().resolvedOptions().timeZone,title:document.querySelector('div.d-input input')?.value||'',content:text(e),
    image_count:Array.from(document.querySelectorAll('.img-preview-area .pr')).filter(visible).length,
    visibility:text(document.querySelector('div.permission-card-wrapper div.d-select-content')),
    schedule:document.querySelector('.date-picker-container input')?.value||'',
    schedule_on:isOn(document.querySelector('.post-time-wrapper .d-switch')),
    original:isOn(card?.querySelector('.d-switch')),rows:rows.map(text),alerts,
    risky:riskElements.length>0 || alerts.some(t=>/验证码|安全验证|操作频繁|访问频繁|账号异常|限制发布|请先登录/.test(t)),
    mention_ids:Array.from(e?.querySelectorAll('[data-user-id],[data-id],a[href*="/user/profile/"]')||[]).map(n=>n.getAttribute('data-user-id')||n.getAttribute('data-id')||(n.getAttribute('href')||'').match(/\/user\/profile\/([^?/#]+)/)?.[1]||'')};
})()"""


def validate_state(state, payload):
    if not isinstance(state, dict) or state.get("risky"):
        raise ValueError("出现登录、安全验证或频率限制，请在 Chrome 中处理后恢复")
    if state.get("title") != payload["title"]:
        raise ValueError("发布页标题与任务不一致")
    compact = lambda s: re.sub(r"\s+", "", str(s))
    if compact(payload["content"]) not in compact(state.get("content", "")):
        raise ValueError("发布页正文与任务不一致")
    if state.get("image_count") != len(payload["images"]):
        raise ValueError("发布页图片数量不一致或上传未完成")
    visibility = payload.get("visibility") or "公开可见"
    if visibility not in state.get("visibility", ""):
        raise ValueError(f"无法确认可见范围为“{visibility}”，已暂停")
    if state.get("original") is None or bool(state["original"]) != bool(payload.get("original", True)):
        raise ValueError("无法确认原创声明开关与任务一致")
    scheduled = payload.get("schedule_time", "")
    if scheduled:
        if payload.get("timezone") and state.get("timezone") != payload["timezone"]:
            raise ValueError("Chrome 时区与计划时区不同，请调整计划时区并重新排期，避免发布错时")
        actual = state.get("schedule", "").replace(" ", "T")[:16]
        if actual != scheduled[:16] or state.get("schedule_on") is False:
            raise ValueError("发布页没有保留任务要求的定时时间")
    elif state.get("schedule_on"):
        raise ValueError("立即发布任务的定时开关仍开启")
    for key, label in (("album", "合集"), ("location", "地点")):
        value = payload.get(key)
        if value and not any(value in row for row in state.get("rows", [])):
            raise ValueError(f"无法确认{label}“{value}”已选中，请在平台核对")
    for tag in payload.get("tags", []):
        if compact(str(tag).lstrip("#")) not in compact(state.get("content", "")):
            raise ValueError(f"话题 #{tag} 未出现在发布正文，已暂停")
    for value in payload.get("mentions", []):
        uid = value.partition("|")[2]
        if not uid or uid not in state.get("mention_ids", []):
            raise ValueError("无法确认 @ 用户已绑定到正确的用户 ID")
    return state


def verify(skill_dir, payload, apply_mentions=False):
    page = page_for(skill_dir)
    if apply_mentions and payload.get("mentions"):
        add_mentions(page, payload["mentions"])
    return validate_state(page.evaluate(FORM_STATE), payload)


def read_management(skill_dir):
    page = page_for(skill_dir)
    page.navigate("https://creator.xiaohongshu.com/new/note-manager")
    page.wait_for_load(timeout=30)
    for _ in range(8):
        cards = page.evaluate(r"""(() => {
          const cards=Array.from(document.querySelectorAll('[class*="note-card"]'));
          return [...new Map(cards.map(e=>[(e.innerText||'').trim(),{
            text:(e.innerText||'').trim(),url:e.querySelector('a[href*="/explore/"],a[href*="/discovery/item/"]')?.href||''}])).values()];
        })()""")
        if cards:
            return cards
        time.sleep(1)
    raise ValueError("笔记管理页面未读取到结果，可能需要重新登录或页面已改版")


def match_receipt(cards, payload):
    from datetime import datetime
    expected = datetime.fromisoformat(payload["schedule_time"])
    matches = []
    for card in cards:
        lines = [line.strip() for line in card.get("text", "").splitlines() if line.strip()]
        if payload["title"] not in lines:
            continue
        dates = [re.search(r"20\d{2}-\d{2}-\d{2}[ T]\d{2}:\d{2}", line) for line in lines]
        dates = [datetime.fromisoformat(m[0]) for m in dates if m]
        if any(abs((dt-expected).total_seconds()) <= 180 for dt in dates):
            matches.append(card)
    if len(matches) != 1:
        return {"status": "unconfirmed", "reason": "未找到唯一匹配标题和时间的笔记"}
    card = matches[0]
    text = card["text"]
    if re.search(r"审核不通过|发布失败|违规|不予通过|已删除", text):
        return {"status": "rejected", "reason": text[:300]}
    if re.search(r"审核中|待审核", text):
        return {"status": "reviewing", "reason": "平台仍在审核"}
    if re.search(r"定时发布|待发布|草稿", text):
        return {"status": "scheduled", "reason": "平台仍显示待发布"}
    if re.search(r"已发布|发布成功", text) or all(word in text for word in ("浏览", "点赞")):
        return {"status": "published", "url": card.get("url", ""), "reason": "管理页匹配到已发布笔记"}
    return {"status": "unconfirmed", "reason": "找到匹配笔记，但无法确认平台最终状态"}
