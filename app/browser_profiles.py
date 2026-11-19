"""Pair Chrome profiles with stable extension clients; never guess connection ownership."""
from __future__ import annotations
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from websockets.sync.client import connect

BRIDGE_PORT = 9333
BRIDGE_VERSION = 4
BRIDGE_URL = f'ws://localhost:{BRIDGE_PORT}'


def control(method, **params):
    # ``_client`` 只用于把命令发给指定 Chrome 用户资料的扩展，不会传到扩展参数里。
    target = params.pop('_client', None)
    message = {'role':'cli', 'method':method, 'params':params}
    if target:
        message['client_id'] = target
    with connect(BRIDGE_URL, open_timeout=2, close_timeout=1, proxy=None) as ws:
        ws.send(json.dumps(message))
        result = json.loads(ws.recv(timeout=5))
    if result.get('error'):
        raise RuntimeError(result['error'])
    return result.get('result') or {}


def _stop_bridge():
    """结束旧版 bridge，改由当前代码启动，避免新命令被旧进程丢弃。"""
    try:
        pids = subprocess.run(['lsof', f'-tiTCP:{BRIDGE_PORT}', '-sTCP:LISTEN'],
                              capture_output=True, text=True, timeout=5).stdout.split()
    except Exception:
        pids = []
    for pid in pids:
        try:
            os.kill(int(pid), signal.SIGTERM)
        except (ValueError, OSError):
            pass
    if pids:
        time.sleep(.4)


def ensure_server(skill_dir):
    try:
        status = control('ping_server')
        if int(status.get('version') or 0) >= BRIDGE_VERSION:
            return status
        _stop_bridge()
    except OSError:
        pass
    subprocess.Popen([sys.executable, str(skill_dir/'scripts'/'bridge_server.py')],
                     cwd=skill_dir, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
    for _ in range(30):
        time.sleep(.1)
        try:
            return control('ping_server')
        except OSError:
            pass
    raise RuntimeError('本地浏览器连接服务未启动')


def reuse_single_connection(skill_dir):
    """Return the sole live client, or ``None`` when reuse is ambiguous.

    The original bridge protocol only supports one extension and does not
    expose client IDs. Protocol 2 exposes all clients, so it is safe to reuse
    an unbound connection only when exactly one client is connected.
    """
    status = ensure_server(skill_dir)
    if not status.get('extension_connected'):
        return None
    if str(status.get('protocol') or '') != '2':
        return ''
    clients = status.get('clients')
    if not isinstance(clients, list) or len(clients) != 1:
        return None
    client = str(clients[0] or '')
    if client and client != 'legacy':
        select(client)
        return client
    return ''


def select(client_id):
    if not client_id:
        raise RuntimeError('这个 Chrome 用户资料尚未绑定扩展，请先切换/连接账号')
    return control('select_client', client_id=client_id)


def supports(client_id, capability):
    """检查已连接的扩展是否支持新命令，避免旧版扩展开出多余页面。"""
    try:
        status = control('ping_server')
    except OSError:
        return False
    caps = status.get('capabilities') or {}
    names = caps.get(client_id)
    if names is None and len(caps) == 1:
        names = next(iter(caps.values()))
    return capability in (names or [])


def open_app_tab(url, client=None):
    """在已连接的 Chrome 用户资料里打开并聚焦工作台页面。"""
    if not supports(client, 'app_open'):
        raise RuntimeError('XHS Bridge 扩展版本过旧，请在 chrome://extensions 重新加载扩展后再切换账号')
    return control('app_open', _client=client, url=url)


def minimize_window(client=None):
    """最小化该 Chrome 用户资料中承载工作台页面的窗口（best-effort）。"""
    if not supports(client, 'window_minimize'):
        raise RuntimeError('XHS Bridge 扩展版本过旧，请在 chrome://extensions 重新加载扩展后再切换账号')
    return control('window_minimize', _client=client)


def pair(profile, skill_dir, seconds=25):
    ensure_server(skill_dir)
    result = control('begin_pair')
    token = result.get('token')
    if not token:
        raise RuntimeError('浏览器连接服务需要重启，扩展也需要更新后重新加载')
    chrome = Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
    if not chrome.exists():
        raise RuntimeError('未找到 Google Chrome')
    subprocess.Popen([str(chrome), f'--profile-directory={profile}', '--new-window',
                      f'http://127.0.0.1:5818/bridge-pair?token={token}'],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic()+seconds
    while time.monotonic()<deadline:
        status = control('pair_status', token=token)
        if status.get('client_id'):
            select(status['client_id'])
            return status['client_id']
        time.sleep(.4)
    raise RuntimeError(f'Chrome「{profile}」未连接。请在该用户资料安装或重新加载新版 XHS Bridge 扩展，再重试。其它 Chrome 窗口已保留。')
