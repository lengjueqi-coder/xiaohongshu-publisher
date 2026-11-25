"""Explicitly bound Chrome tabs for the multi-platform publisher (no cookie export)."""
from __future__ import annotations
import json
from contextlib import contextmanager
from contextvars import ContextVar
import time
import uuid
from pathlib import Path
from websockets.sync.client import connect
import browser_profiles

DOM_PATH = Path(__file__).with_name('static') / 'platform-dom.js'
URLS = {
    'douyin': {'image': 'https://creator.douyin.com/creator-micro/content/upload?default-tab=3', 'video': 'https://creator.douyin.com/creator-micro/content/upload'},
    'channels': {'image': 'https://channels.weixin.qq.com/platform/post/finderNewLifeCreate', 'video': 'https://channels.weixin.qq.com/platform/post/create'},
}

_sessions = ContextVar('platform_background_sessions', default=None)


@contextmanager
def background_session():
    if _sessions.get() is not None:
        yield
        return
    sessions = {}
    context = _sessions.set(sessions)
    try:
        yield
    finally:
        _sessions.reset(context)
        for (client, platform, tab_id), token in sessions.items():
            try:
                rpc(client, platform, 'background_end', tab_id=tab_id, session=token)
            except Exception:
                # The extension also releases idle sessions after a lost connection.
                pass


def rpc(client, platform, action, **params):
    if platform not in URLS:
        raise ValueError('不支持的平台')
    if not client:
        raise ValueError('请先连接官方页面')
    if action in ('open_background','background_begin'):
        capabilities = browser_profiles.control('ping_server').get('capabilities', {})
        if 'platforms_background_v1' not in capabilities.get(client, []):
            raise ValueError('请在对应 Chrome 用户资料的扩展管理页重新加载新版 XHS Bridge，再继续后台发布')
    with connect(browser_profiles.BRIDGE_URL, open_timeout=3, close_timeout=1, proxy=None, max_size=32*1024*1024) as ws:
        ws.send(json.dumps({'role':'cli', 'client_id':client, 'method':'platform', 'params':dict(platform=platform, action=action, **params)}))
        response = json.loads(ws.recv(timeout=90))
    if response.get('error'):
        raise RuntimeError(response['error'])
    return response.get('result')


def tabs():
    status = browser_profiles.control('ping_server')
    found = []
    for client in status.get('clients', []):
        if 'platforms_v1' not in status.get('capabilities', {}).get(client, []):
            continue
        for platform in URLS:
            for tab in rpc(client, platform, 'tabs'):
                found.append(dict(tab, client_id=client, platform=platform))
    return found


class Page:
    def __init__(self, binding):
        self.platform = binding['platform']
        self.client = binding['client_id']
        self.tab_id = int(binding['tab_id'])

    def call(self, action, **params):
        sessions = _sessions.get()
        key = (self.client, self.platform, self.tab_id)
        if sessions and key in sessions:
            params['session'] = sessions[key]
        return rpc(self.client, self.platform, action, tab_id=self.tab_id, **params)

    def background_input(self):
        sessions = _sessions.get()
        if sessions is None:
            raise RuntimeError('后台页面操作缺少会话')
        key = (self.client, self.platform, self.tab_id)
        if key not in sessions:
            token = uuid.uuid4().hex
            rpc(self.client, self.platform, 'background_begin', tab_id=self.tab_id, session=token)
            sessions[key] = token

    def dom(self, action, **params):
        source = DOM_PATH.read_text('utf-8')
        return self.call('evaluate', expression='(' + source + ')(' + json.dumps(dict(platform=self.platform, action=action, **params), ensure_ascii=False) + ')')

    def click(self, **locator):
        self.dom('dismiss_guidance')
        for attempt in range(12):
            try:
                point = self.dom('point', **locator)
                break
            except RuntimeError as exc:
                if '未找到' not in str(exc) or attempt == 11:
                    raise
                time.sleep(.25)
        self.call('click', **point)
        time.sleep(.15)

    def text(self, field, value):
        self.dom('text', field=field, value=value)
        time.sleep(.1)

    def editor(self, content, tags):
        # Verify native topic nodes before Escape/blur. At most one complete
        # rewrite is allowed, never an extra publish attempt.
        for attempt in range(2):
            try:
                self._editor_once(content,tags)
                return
            except ValueError as exc:
                if str(exc)!='官方话题尚未成为有效节点' or attempt:raise

    def _editor_once(self, content, tags):
        self.background_input()
        self.dom('dismiss_guidance')
        self.click(field='content')
        self.call('key', key='SelectAll')
        time.sleep(.5)
        self.call('key', key='Backspace')
        time.sleep(.3)
        if self.snapshot()['fields']['content']:
            raise ValueError('官方编辑器未清空，已停止，避免叠加旧文案')
        self.call('insert_text', text=content)
        time.sleep(.7)
        for tag in tags:
            self.call('key', key='Space')
            self.call('insert_text', text=' ')
            time.sleep(.3)
            self.call('insert_text', text='#')
            time.sleep(.5)
            self.call('insert_text', text=tag)
            if self.platform == 'douyin':
                for attempt in range(12):
                    if self.dom('topic_exists', value=tag):
                        break
                    time.sleep(.25)
                else:
                    # Some editor builds commit the first composition before
                    # starting topic search. Re-enter only this unselected token.
                    for _ in range(len(tag)+1):self.call('key',key='Backspace')
                    time.sleep(.3)
                    self.call('insert_text',text='#');time.sleep(.5)
                    self.call('insert_text',text=tag)
                    for _ in range(12):
                        if self.dom('topic_exists',value=tag):break
                        time.sleep(.25)
                # Long captions can scroll this suggestion outside the viewport.
                # Activate the exact official suggestion, then check its node.
                self.dom('activate',selector='[class*=tag-hash-view-name]',text=tag)
                for _ in range(12):
                    native=self.dom('snapshot')['fields']['tags']
                    if '#'+tag in native:break
                    time.sleep(.2)
                else:raise ValueError('官方话题尚未成为有效节点')
            else:
                time.sleep(.3)
                self.call('key', key='Space')
                self.call('insert_text', text=' ')
            time.sleep(.15)
        self.call('key', key='Escape')
        self.dom('blur_editor')
        time.sleep(.2)

    def choose(self, field, value):
        if field == 'activity' and self.platform == 'douyin':
            self.dom('set_activity', value=value)
            time.sleep(.3)
            return
        if self.dom('read_field', field=field) == value and value not in ('内容为自行拍摄','内容为转载'):
            return
        self.background_input()
        if (self.platform == 'douyin' and (field,value) in [('music','不添加音乐'),('location','不显示位置'),('hotspot','不关联热点')]) or (self.platform=='channels' and field=='collection' and value=='不选择合集'):
            if field in ('location','hotspot'):self.click(field=field)
            self.dom('clear_field',field=field)
            time.sleep(.4)
            self.call('key',key='Escape')
            return
        # A preceding option read may have left this dropdown open.
        self.call('key',key='Escape')
        self.click(field='title' if self.dom('read_field',field='title') is not None else 'short_title')
        self.click(field=field)
        time.sleep(.4)
        if self.platform=='douyin' and field=='music':
            for _ in range(10):
                if self.dom('options',field=field):break
                time.sleep(.2)
        try:
            self.dom('activate', option=value, field=field)
        except RuntimeError as exc:
            if field not in ('location','music','hotspot') or '未找到' not in str(exc):
                raise
            self.dom('search_option', field=field, value=value)
            if self.platform=='douyin' and field=='music':
                self.call('key',key='Enter')
            for _ in range(12):
                time.sleep(.25)
                if any(o['label']==value for o in self.dom('options',field=field)):break
            self.dom('activate', option=value, field=field)
        time.sleep(.2)
        if field == 'declaration' and self.platform == 'douyin':
            self.click(button='确定', dialog=True)
        time.sleep(.15)

    def radio(self, value):
        if not self.dom('radio_checked', value=value):
            self.click(radio=value)
        if not self.dom('radio_checked', value=value):
            raise ValueError('官方页面未选中：' + value)

    def snapshot(self, fresh_identity=False):
        snapshot = self.dom('snapshot')
        # Read the authenticated account, never infer identity from old draft keys.
        cached = getattr(self, '_identity_cache', None)
        if fresh_identity or not cached or time.monotonic() - cached[0] > 15:
            identity = self.dom('identity')
            self._identity_cache = (time.monotonic(), identity)
        else:
            identity = cached[1]
        snapshot.update(identity)
        return snapshot

    def schedule(self, value):
        """Drive calendar widgets, including minutes; verify after blur.

        Widget selectors adapted from laoshalab/social-auto-upload-web-ui-pro
        (MIT); see vendor/platform-adapters/LICENSE and NOTICE.md.
        """
        from datetime import datetime
        when = datetime.strptime(value, '%Y-%m-%d %H:%M')
        self.background_input()
        self.radio('定时发布' if self.platform == 'douyin' else '定时')
        self.click(field='schedule_time')
        time.sleep(.6)
        if self.platform == 'douyin':
            selector = f'.semi-datepicker-day:not(.semi-datepicker-day-disabled)[title="{when:%Y-%m-%d}"]'
            for attempt in range(3):
                if self.dom('exists', selector=selector):
                    break
                self.click(selector='.semi-datepicker-navigation > button', index=1)
            self.click(selector=selector)
            self.click(selector='.semi-datepicker-switch-time')
            # Text may have a 时 / 分 suffix on the selected wheel item.
            for name, number in [('hour', when.hour), ('minute', when.minute)]:
                self.dom('activate', selector=f'.semi-scrolllist-item-wheel.undefined-list-{name} li', number=number, index=0)
                time.sleep(.3)
            if self.dom('exists', selector='.semi-datepicker-footer button'):
                self.click(selector='.semi-datepicker-footer button', text='确定')
            else:
                self.click(field='title')
        else:
            for attempt in range(3):
                month = self.dom('calendar_month')
                if month == [when.year, when.month]:
                    break
                if not month or month > [when.year, when.month]:
                    self.click(selector='.weui-desktop-picker__panel__hd .weui-desktop-btn__icon__left')
                else:
                    self.click(selector='.weui-desktop-picker__panel__hd .weui-desktop-btn__icon__right')
            self.click(selector='table.weui-desktop-picker__table a:not(.weui-desktop-picker__disabled):not(.weui-desktop-picker__faded)', text=str(when.day))
            self.click(selector='input[placeholder="请选择时间"]')
            self.dom('text', selector='input[placeholder="请选择时间"]', value=when.strftime('%H:%M'))
            self.click(field='title' if self.dom('exists',selector='input[placeholder*="填写标题"]') else 'short_title')
        time.sleep(.3)
        actual = self.dom('read_field', field='schedule_time')
        if actual != value:
            raise ValueError(f'定时读回不一致：期望 {value}，官方页面为 {actual}')

    def verify_repost_origin(self):
        self.background_input()
        self.click(field='declaration')
        try:
            self.wait_repost_origin()
        finally:
            self.click(button='取消', dialog=True)

    def wait_repost_origin(self):
        for _ in range(12):
            if self.dom('external_repost_selected'):
                return
            time.sleep(.15)
        raise ValueError('抖音没有确认转载声明为取材站外')

    def declaration(self, payload):
        if self.platform=='douyin' and payload['declaration']=='内容为转载信息':
            self.background_input()
            self.click(field='declaration')
            self.dom('activate', option='内容为转载信息', field='declaration')
            self.click(radio='取材站外')
            self.wait_repost_origin()
            self.click(button='确定', dialog=True)
            time.sleep(.4)
            if self.dom('exists', selector='.semi-modal-content'):
                raise ValueError('抖音转载声明弹窗尚未确认')
            return
        self.choose('declaration', payload['declaration'])
        if self.platform != 'channels':
            return
        if payload['declaration'] == '内容为自行拍摄':
            self.dom('text', selector='input[placeholder="请选择拍摄时间"]', value=payload['shoot_date'])
            self.click(selector='.location-cascader .weui-desktop-form__dropdowncascade__dt__inner-button')
            for level in payload['shoot_region']:
                self.dom('activate', selector='.location-cascader .weui-desktop-dropdown__list-ele__text', text=level)
                time.sleep(.3)
            self.click(button='确定', dialog=True)
        elif payload['declaration'] == '内容为转载':
            self.dom('text', selector='textarea[placeholder*="转载来源"]', value=payload.get('repost_source',''))
            self.click(button='完成', dialog=True)
