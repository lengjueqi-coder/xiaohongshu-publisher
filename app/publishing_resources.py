"""Per-account local templates and conservative reads of platform selectors."""
from __future__ import annotations
import re
import sys
import time


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
    tags = list(dict.fromkeys(piece.strip() for piece in pieces if piece.strip()))
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
        match.strip() for match in re.findall(r'#([^#\s,，;；、]+)', detail) if match.strip()
    ))[:10]
    return topics, detail


_ALBUM_STATE_JS = r'''(() => {
  const visible=e=>!!e&&e.getBoundingClientRect().width>0&&e.getBoundingClientRect().height>0&&getComputedStyle(e).visibility!=='hidden';
  const dialogs=[...document.querySelectorAll('[role="dialog"],.d-modal,.d-dialog,[role="listbox"],.d-dropdown')].filter(visible);
  const modal=dialogs.find(e=>/合集|专辑/.test(e.innerText||''));
  if(!modal)return {found:false,albums:[],moved:false};
  const rows=[...modal.querySelectorAll('[role="option"],label,[class*="album-name"],[class*="collection-name"],[class*="item-title"]')].filter(visible);
  const albums=[...new Set(rows.map(e=>(e.innerText||'').trim()).filter(t=>t&&t.length<=100&&!/^(创建|新建|确定|取消|搜索)/.test(t)))];
  const scrollables=[modal,...modal.querySelectorAll('*')].filter(visible).filter(e=>e.scrollHeight>e.clientHeight+10);
  const sc=scrollables.pop();const before=sc?.scrollTop;
  if(sc)sc.scrollTop+=Math.max(200,sc.clientHeight*.8);
  return {found:true,albums,moved:!!sc&&sc.scrollTop>before};
})()'''


def read_resources(skill_dir):
    scripts = str(skill_dir/'scripts')
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    from xhs.bridge import BridgePage
    from xhs.publish import _click_topic_template_entry, _template_state, _close_topic_template
    page = BridgePage()
    url = page.evaluate('location.href')
    if 'creator.xiaohongshu.com' not in url or not page.has_element('.ql-editor'):
        raise ValueError('请先填入一组素材到当前账号的小红书发布页，再同步模板和合集。同步不会改写标题正文。也可直接使用本地话题模板。')
    result = {
        'templates': [],
        'albums': [],
        'status': {
            'templates': {'ok': False, 'count': 0, 'topics_complete': False, 'message': ''},
            'albums': {'ok': False, 'count': 0, 'message': ''},
        },
        'errors': [],
    }
    template_closed = True
    try:
        if not _click_topic_template_entry(page):
            raise ValueError('未找到平台话题模板入口')
        for _ in range(10):
            if _template_state(page).get('found'):
                break
            time.sleep(.2)
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
            if not _template_state(page, 'scroll').get('moved'):
                break
            time.sleep(.15)
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
        try:
            _close_topic_template(page)
        except Exception as exc:
            template_closed = False
            message = f'平台话题模板弹窗未能关闭：{exc}'
            result['status']['templates']['ok'] = False
            result['status']['templates']['message'] = message
            result['errors'].append(message)
    # Read the full scrollable album list; never type into a fallback title input.
    if not template_closed:
        result['status']['albums']['message'] = '话题模板弹窗未关闭，未继续读取合集'
        result['errors'].append(result['status']['albums']['message'])
        return result
    opened = page.evaluate(r'''(() => {
      const visible=e=>e.getBoundingClientRect().width>0;
      const nodes=[...document.querySelectorAll('button,span,[role="button"]')].filter(visible);
      const entry=nodes.find(e=>/^(加入合集|添加到合集|选择合集|加入专辑)$/.test((e.innerText||'').trim()));
      if(!entry)return false;entry.click();return true;
    })()''')
    if opened:
        try:
            seen_albums = []
            found = False
            for _ in range(30):
                state = page.evaluate(_ALBUM_STATE_JS) or {}
                if state.get('found'):
                    found = True
                    seen_albums.extend(state.get('albums') or [])
                    if not state.get('moved'):
                        break
                elif not found:
                    time.sleep(.2)
                    continue
                else:
                    break
                time.sleep(.15)
            if not found:
                raise ValueError('未读到可确认的合集弹窗')
            result['albums'] = list(dict.fromkeys(seen_albums))
            result['status']['albums'] = {
                'ok': True,
                'count': len(result['albums']),
                'message': (
                    f'已读取 {len(result["albums"])} 个合集'
                    if result['albums'] else '合集列表读取成功，当前账号没有可用合集'
                ),
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
    return result
