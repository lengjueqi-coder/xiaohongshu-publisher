"""Pair Chrome profiles with stable extension clients; never guess connection ownership."""
from __future__ import annotations
import json
import subprocess
import sys
import time
from pathlib import Path
from websockets.sync.client import connect


def control(method, **params):
    with connect('ws://localhost:9333', open_timeout=2, close_timeout=1, proxy=None) as ws:
        ws.send(json.dumps({'role':'cli', 'method':method, 'params':params}))
        result = json.loads(ws.recv(timeout=5))
    if result.get('error'):
        raise RuntimeError(result['error'])
    return result.get('result') or {}


def ensure_server(skill_dir):
    try:
        return control('ping_server')
    except OSError:
        subprocess.Popen([sys.executable, str(skill_dir/'scripts'/'bridge_server.py')],
                         cwd=skill_dir, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
        for _ in range(20):
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
