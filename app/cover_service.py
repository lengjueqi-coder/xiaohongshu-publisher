"""API易 Nano Banana image editing with verified TLS and explicit failures."""
from __future__ import annotations

import base64
import io
import json
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from PIL import Image, ImageOps

MODELS = {
    'gemini-3.1-flash-image-preview': ('1k', '2k', '4k'),
    'gemini-3-pro-image': ('2k', '4k'),
}
RATIOS = {'1:1', '2:3', '3:2', '3:4', '4:3', '4:5', '5:4', '9:16', '16:9', '21:9'}


class CoverError(ValueError):
    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


def normalize_base_url(value: str) -> str:
    value = str(value or 'https://api.apiyi.com').strip().rstrip('/')
    parts = urllib.parse.urlsplit(value)
    if parts.scheme != 'https' or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
        raise CoverError('API易接入地址必须是 HTTPS 地址，不含账号、查询参数或片段', 400)
    path = re.sub(r'/v1(?:beta)?(?:/.*)?$', '', parts.path).rstrip('/')
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, path, '', ''))


def safe_detail(value: object, api_key: str = '') -> str:
    text = str(value)
    if api_key:
        text = text.replace(api_key, '[密钥已隐藏]')
    text = re.sub(r'sk-[\w-]+', '[密钥已隐藏]', text)
    text = re.sub(r'(?i)Bearer\s+[^\s"\']+', 'Bearer [密钥已隐藏]', text)
    return text[:350]


def generate_cover(payload: dict, api_key: str, base_url: str, ssl_context: ssl.SSLContext) -> dict:
    api_key = str(api_key or '').strip()
    if not api_key:
        raise CoverError('API易 Key 未配置，请到后台设置 → AI 封面填写并保存', 400)
    base_url = normalize_base_url(base_url)
    model = str(payload.get('model') or 'gemini-3.1-flash-image-preview').strip()
    resolution = str(payload.get('resolution') or '2k').strip().lower()
    ratio = str(payload.get('aspect_ratio') or '3:4').strip()
    prompt = str(payload.get('prompt') or '').strip()
    if model not in MODELS or resolution not in MODELS[model]:
        raise CoverError('当前模型或分辨率不受支持，请重新选择', 400)
    if ratio not in RATIOS:
        raise CoverError('不支持此封面比例', 400)
    if not prompt:
        raise CoverError('提示词不能为空', 400)
    src = Path(str(payload.get('image_path') or ''))
    if not src.is_file():
        raise CoverError('参考图不存在，请重新选择素材', 400)
    try:
        with Image.open(src) as original:
            image = ImageOps.exif_transpose(original).convert('RGB')
            image.thumbnail((2048, 2048))
            buf = io.BytesIO()
            image.save(buf, 'JPEG', quality=90)
    except (OSError, ValueError) as exc:
        raise CoverError('无法读取参考图，请选择有效图片', 400) from exc
    body = {
        'contents': [{'parts': [{'text': prompt}, {'inlineData': {
            'mimeType': 'image/jpeg', 'data': base64.b64encode(buf.getvalue()).decode('ascii')}}]}],
        'generationConfig': {'imageConfig': {'imageSize': resolution.upper(), 'aspectRatio': ratio}},
    }
    request = urllib.request.Request(
        f'{base_url}/v1beta/models/{model}:generateContent',
        data=json.dumps(body).encode('utf-8'),
        headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'}, method='POST',
    )
    started = time.monotonic()
    try:
        # 4K can take several minutes; match the source application's timeout.
        with urllib.request.urlopen(request, context=ssl_context, timeout=600 if resolution == '4k' else 300) as response:
            data = json.loads(response.read().decode('utf-8'))
    except urllib.error.HTTPError as exc:
        detail = safe_detail(exc.read().decode('utf-8', errors='replace'), api_key)
        advice = {
            401: 'Key 无效或已失效，请在后台设置更新 API易 Key 并保存',
            402: '账户余额不足，请检查 API易余额',
            403: '没有此模型的访问权限，请检查 API易令牌分组和模型权限',
            429: '请求频率或额度受限，请稍后重试并检查 API易额度',
        }.get(exc.code, '服务暂时不可用，请检查 API易平台状态')
        raise CoverError(f'API易 HTTP {exc.code}：{advice}。{detail}') from exc
    except (TimeoutError, urllib.error.URLError) as exc:
        reason = getattr(exc, 'reason', exc)
        if isinstance(reason, ssl.SSLCertVerificationError):
            message = 'HTTPS 证书验证失败，请检查本机证书或代理配置'
        elif isinstance(reason, TimeoutError):
            message = '请求超时；上游可能仍在生成，请先检查 API易调用记录再重试，避免重复计费'
        else:
            message = '网络连接失败，请检查网络或代理后重试'
        raise CoverError(f'API易{message}。{safe_detail(reason, api_key)}') from exc
    except (ValueError, UnicodeError) as exc:
        raise CoverError('API易返回的内容不是有效 JSON，请稍后重试') from exc
    if not isinstance(data, dict):
        raise CoverError('API易返回的数据格式异常')
    if data.get('error'):
        raise CoverError('API易生成失败：' + safe_detail(data['error'], api_key))
    candidates = data.get('candidates') or []
    candidate = candidates[0] if candidates and isinstance(candidates[0], dict) else {}
    parts = (candidate.get('content') or {}).get('parts') or []
    images = []
    for part in parts:
        if not isinstance(part, dict) or part.get('thought'):
            continue
        inline = part.get('inlineData') or part.get('inline_data') or {}
        if isinstance(inline, dict) and inline.get('data'):
            images.append(inline['data'])
    if not images:
        finish = str(candidate.get('finishReason') or candidate.get('finish_reason') or '').upper()
        block = (data.get('promptFeedback') or {}).get('blockReason')
        if block or finish in {'SAFETY', 'IMAGE_SAFETY', 'IMAGE_PROHIBITED_CONTENT', 'PROHIBITED_CONTENT', 'BLOCKLIST'}:
            raise CoverError('API易上游审核拦截（' + safe_detail(block or finish, api_key) + '），请调整提示词或参考图')
        raise CoverError('API易未返回图片（finishReason=' + safe_detail(finish or 'UNKNOWN', api_key) + '），请检查调用记录后重试')
    encoded = images[-1]
    try:
        raw = base64.b64decode(encoded, validate=True)
        with Image.open(io.BytesIO(raw)) as result:
            width, height = result.size
            mime = Image.MIME.get(result.format, 'image/png')
            result.verify()
    except (ValueError, OSError, TypeError) as exc:
        raise CoverError('API易返回的图片数据无效，请检查调用记录后重试') from exc
    return {'ok': True, 'b64': encoded, 'mime': mime, 'width': width, 'height': height,
            'model': model, 'resolution': resolution, 'elapsed_sec': round(time.monotonic() - started, 2)}
