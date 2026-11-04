"""图像叠加与拼图模块：logo/水印合成、实时预览、拼图。

依赖 Pillow；未安装时功能自动降级（发布链路跳过合成，用原图）。
"""
from __future__ import annotations

import io
import os
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    from PIL import Image, ImageOps

    HAS_PIL = True
except ImportError:  # pragma: no cover
    Image = ImageOps = None  # type: ignore
    HAS_PIL = False

APP_ROOT = Path(__file__).resolve().parents[1]
ASSETS_DIR = APP_ROOT / "data" / "assets"
BACKUP_DIR = APP_ROOT / "data" / "backup"
PREVIEW_MAX = 1600
COLLAGE_MAX_SIDE = 1242  # 小红书 3:4 竖版宽度基准

# 九宫格位置
POSITIONS = {"tl", "tc", "tr", "ml", "center", "mr", "bl", "bc", "br"}

# 拼图布局：名称 -> (列, 行)
LAYOUTS = {
    "1x2": (1, 2),
    "2x1": (2, 1),
    "2x2": (2, 2),
    "3x3": (3, 3),
    "2x3": (2, 3),
    "3x2": (3, 2),
}

DEFAULT_PARAMS: dict[str, Any] = {
    "fx_enabled": True,
    "logo_enabled": False,
    "logo_path": "",
    "logo_position": "br",
    "logo_margin": 3,      # 相对短边百分比
    "logo_scale": 18,      # 相对短边百分比
    "watermark_enabled": False,
    "watermark_path": "",
    "watermark_position": "center",
    "watermark_opacity": 40,
    "watermark_scale": 60,
    "output_mode": "merge",  # merge | replace | suffix
}


def load_overlay_params(overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    """合并 默认值 + db settings + 本次覆盖。db 读取由 server 注入（避免循环依赖）。"""
    params = dict(DEFAULT_PARAMS)
    if overrides:
        for key in DEFAULT_PARAMS:
            if key in overrides and overrides[key] is not None:
                params[key] = overrides[key]
    return params


def _clamp(value: float, low: int, high: int) -> float:
    return max(low, min(high, value))


def _load_overlay(path: str) -> Image.Image | None:
    if not path:
        return None
    p = Path(path)
    if not p.exists() or not p.is_file():
        return None
    img = Image.open(p)
    if img.mode not in ("RGBA", "LA"):
        img = img.convert("RGBA")
    return img


def _paste_at(base: Image.Image, overlay: Image.Image, position: str, margin_px: int) -> None:
    """把 overlay（已缩放）贴到 base 上按九宫格位置。原地修改。"""
    bw, bh = base.size
    ow, oh = overlay.size
    # 自由坐标："垂直%,水平%"（中心点百分比），来自预览滑杆，保证所见即所得
    m = re.match(r"^\s*([\d.]+)\s*%\s*,\s*([\d.]+)\s*%\s*$", str(position))
    if m:
        fx = _clamp(float(m.group(1)), 0, 100) / 100
        fy = _clamp(float(m.group(2)), 0, 100) / 100
        x, y = int(bw * fx - ow / 2), int(bh * fy - oh / 2)
        base.paste(overlay, (x, y), overlay)
        return
    if position == "tl":
        x, y = margin_px, margin_px
    elif position == "tc":
        x, y = (bw - ow) // 2, margin_px
    elif position == "tr":
        x, y = bw - ow - margin_px, margin_px
    elif position == "ml":
        x, y = margin_px, (bh - oh) // 2
    elif position == "mr":
        x, y = bw - ow - margin_px, (bh - oh) // 2
    elif position == "bl":
        x, y = margin_px, bh - oh - margin_px
    elif position == "bc":
        x, y = (bw - ow) // 2, bh - oh - margin_px
    elif position == "br":
        x, y = bw - ow - margin_px, bh - oh - margin_px
    else:  # center
        x, y = (bw - ow) // 2, (bh - oh) // 2
    base.paste(overlay, (x, y), overlay)


def _apply_watermark_alpha(overlay: Image.Image, opacity: int) -> Image.Image:
    """opacity 0-100，缩放 alpha 通道。"""
    if opacity >= 100:
        return overlay
    img = overlay.copy()
    alpha = img.getchannel("A").point(lambda a: int(a * _clamp(opacity, 0, 100) / 100))
    img.putalpha(alpha)
    return img


def _font(size: int):
    """水印字体：Hiragino Sans GB W3 优先（纤细精致），失败回退。"""
    for path, idx in (
        ("/System/Library/Fonts/Hiragino Sans GB.ttc", 0),
        ("/System/Library/Fonts/PingFang.ttc", 0),
    ):
        try:
            from PIL import ImageFont

            return ImageFont.truetype(path, max(8, int(size)), index=idx)
        except Exception:
            continue
    from PIL import ImageFont

    return ImageFont.load_default()


def _draw_dash_line(draw, p1, p2, dash, gap, width, fill):
    import math

    x1, y1 = p1
    x2, y2 = p2
    length = math.hypot(x2 - x1, y2 - y1)
    if length <= 0:
        return
    ux, uy = (x2 - x1) / length, (y2 - y1) / length
    d = 0.0
    while d < length:
        e = min(d + dash, length)
        draw.line([(x1 + ux * d, y1 + uy * d), (x1 + ux * e, y1 + uy * e)], fill=fill, width=width)
        d = e + gap


def draw_text_grid_watermark(base: Image.Image, text: str, scale_pct: float, opacity: int) -> None:
    """斜45度交叉网格虚线水印：交叉点放文字。原地修改。
    scale_pct：网格密度（相对短边百分比，与前端预览共用同一映射）。"""
    import math

    from PIL import ImageDraw

    if not text.strip():
        return
    w, h = base.size
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    short = min(w, h)
    cell = max(60.0, short * _clamp(float(scale_pct), 1, 300) / 100 * 0.22)
    font_size = max(10, int(cell * 0.13))
    font = _font(font_size)
    la = int(255 * _clamp(int(opacity), 0, 100) / 100 * 0.62)  # 线比字略淡
    ta = int(255 * _clamp(int(opacity), 0, 100) / 100)
    white = (255, 255, 255, la)
    total = w + h
    k = -total
    while k < total:
        _draw_dash_line(d, (k, 0), (k + h, h), dash=cell * 0.045, gap=cell * 0.032, width=1, fill=white)
        _draw_dash_line(d, (k, 0), (k - h, h), dash=cell * 0.045, gap=cell * 0.032, width=1, fill=white)
        k += cell
    bbox = d.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    # 预生成单条 45° 旋转文字贴图（左低右高，与 / 方向网格线平齐）
    text_img = Image.new("RGBA", (tw + 8, th + 8), (0, 0, 0, 0))
    ImageDraw.Draw(text_img).text((4 - bbox[0], 4 - bbox[1]), text, font=font, fill=(255, 255, 255, ta))
    text_img = text_img.rotate(45, expand=True, resample=Image.BICUBIC)
    rw, rh = text_img.size
    diag = int(math.hypot(w, h))
    step = int(cell)
    for i in range(-diag // step - 2, diag // step + 3):
        for j in range(-diag // step - 2, diag // step + 3):
            u, v = i * cell, j * cell
            x = (u - v) / math.sqrt(2)
            y = (u + v) / math.sqrt(2)
            if -step <= x <= w + step and -step <= y <= h + step:
                layer.paste(text_img, (int(x - rw / 2), int(y - rh / 2)), text_img)
    base.paste(layer, (0, 0), layer)


def _apply_overlays(base: Image.Image, params: dict[str, Any]) -> Image.Image:
    """在 base 上按参数叠加 logo 和水印（原地）。"""
    short_side = min(base.size)
    margin = int(short_side * _clamp(float(params.get("logo_margin", 3)), 0, 30) / 100)

    if params.get("logo_enabled") and params.get("logo_path"):
        logo = _load_overlay(params["logo_path"])
        if logo is not None:
            target = max(8, int(short_side * _clamp(float(params.get("logo_scale", 18)), 1, 100) / 100))
            ratio = target / min(logo.size)
            logo = logo.resize((max(1, int(logo.width * ratio)), max(1, int(logo.height * ratio))), Image.LANCZOS)
            _paste_at(base, logo, str(params.get("logo_position", "br")), margin)

    # 文字网格水印优先：有文字就用软件自动生成，忽略上传的 PNG
    if params.get("watermark_enabled") and str(params.get("watermark_text", "")).strip():
        draw_text_grid_watermark(
            base,
            str(params.get("watermark_text")),
            float(params.get("watermark_scale", 120)),
            int(_clamp(float(params.get("watermark_opacity", 40)), 0, 100)),
        )
    elif params.get("watermark_enabled") and params.get("watermark_path"):
        wm = _load_overlay(params["watermark_path"])
        if wm is not None:
            target = int(short_side * _clamp(float(params.get("watermark_scale", 60)), 1, 200) / 100)
            ratio = target / min(wm.size)
            wm = wm.resize((max(1, int(wm.width * ratio)), max(1, int(wm.height * ratio))), Image.LANCZOS)
            wm = _apply_watermark_alpha(wm, int(_clamp(float(params.get("watermark_opacity", 40)), 0, 100)))
            _paste_at(base, wm, str(params.get("watermark_position", "center")), margin)
    return base


def has_overlays(params: dict[str, Any]) -> bool:
    has_wm = (str(params.get("watermark_text", "")).strip() or params.get("watermark_path")) if params.get("watermark_enabled") else False
    return bool(
        params.get("fx_enabled")
        and ((params.get("logo_enabled") and params.get("logo_path")) or has_wm)
    )


def compose_one(src: Path, params: dict[str, Any], max_dim: int | None = None) -> Image.Image:
    """打开原图 → 叠 logo → 叠水印。返回 PIL Image（不落盘）。"""
    base = Image.open(src)
    if base.mode != "RGB":
        base = base.convert("RGB")
    if max_dim:
        base = ImageOps.contain(base, (max_dim, max_dim))
    return _apply_overlays(base, params)


def render_preview(src: str | Path, params: dict[str, Any]) -> bytes:
    """合成预览：最长边 PREVIEW_MAX，JPEG bytes。预览含水印透明度实际效果。"""
    img = compose_one(Path(src), params, max_dim=PREVIEW_MAX)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=88)
    return buf.getvalue()


def _output_target(src: Path, mode: str, prepared_dir: Path) -> tuple[Path, bool]:
    """返回 (输出路径, 是否覆盖原图)。"""
    if mode == "replace":
        return src, True
    if mode == "suffix":
        return src.with_name(f"{src.stem}-logo{src.suffix.lower() if src.suffix.lower() in ('.jpg', '.jpeg') else '.jpg'}"), False
    return prepared_dir / f"{src.stem}-logo.jpg", False


def apply_for_publish(
    images: list[str],
    params: dict[str, Any],
    prepared_dir: Path,
) -> tuple[list[str], list[str]]:
    """按 output_mode 合成，返回 (图片路径列表, 提示列表)。失败的单张回退原图。"""
    if not HAS_PIL:
        return images, ["未安装 Pillow，已跳过 logo/水印合成（pip install Pillow 后可用）"]
    if not params.get("fx_enabled"):
        return images, []
    has_overlay = (params.get("logo_enabled") and params.get("logo_path")) or (
        params.get("watermark_enabled") and params.get("watermark_path")
    )
    if not has_overlay:
        return images, []

    mode = str(params.get("output_mode", "merge"))
    notes: list[str] = []
    out_paths: list[str] = []
    backup_subdir: Path | None = None

    if mode == "replace":
        backup_subdir = BACKUP_DIR / datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_subdir.mkdir(parents=True, exist_ok=True)
        notes.append(f"replace 模式：原图已备份到 {backup_subdir}")

    prepared_dir.mkdir(parents=True, exist_ok=True)
    for raw in images:
        src = Path(raw)
        if not src.exists() or not src.is_file():
            out_paths.append(raw)
            continue
        try:
            target, overwrite = _output_target(src, mode, prepared_dir)
            if overwrite and backup_subdir is not None:
                shutil.copy2(src, backup_subdir / src.name)
            composed = compose_one(src, params)
            tmp = target.with_name(f".{target.stem}-{os.getpid()}.tmp.jpg")
            composed.save(tmp, format="JPEG", quality=92)
            if overwrite:
                os.replace(tmp, target)
            else:
                if tmp.exists():
                    tmp.rename(target)
                elif not target.exists():
                    composed.save(target, format="JPEG", quality=92)
            out_paths.append(str(target))
        except Exception as exc:  # 单张失败不阻塞发布
            out_paths.append(raw)
            notes.append(f"{src.name} 合成失败已用原图：{exc}")
    return out_paths, notes


def make_collage(
    images: list[str],
    layout: str,
    gap: int,
    bg: str,
    out_path: Path,
    params: dict[str, Any] | None = None,
) -> Path:
    """固定网格拼图，3:4 竖版，居中裁剪填满单元格，最后叠加 logo/水印。"""
    if not HAS_PIL:
        raise RuntimeError("未安装 Pillow，无法拼图（请在终端执行 pip install Pillow）")
    if layout not in LAYOUTS:
        raise ValueError(f"不支持的布局：{layout}，可选 {'/'.join(LAYOUTS)}")
    cols, rows = LAYOUTS[layout]
    paths = [Path(p) for p in images[: cols * rows]]
    if len(paths) < 2:
        raise ValueError("拼图至少需要 2 张图片")

    gap = int(_clamp(gap, 0, 40))
    cell_w = (COLLAGE_MAX_SIDE - gap * (cols - 1)) // cols
    cell_h = int(cell_w * 4 / 3)
    W = cell_w * cols + gap * (cols - 1)
    H = cell_h * rows + gap * (rows - 1)

    canvas = Image.new("RGB", (W, H), bg or "#ffffff")
    for idx, p in enumerate(paths):
        col, row = idx % cols, idx // cols
        x = col * (cell_w + gap)
        y = row * (cell_h + gap)
        cell = ImageOps.fit(Image.open(p).convert("RGB"), (cell_w, cell_h), Image.LANCZOS)
        canvas.paste(cell, (x, y))

    if params and has_overlays(params):
        _apply_overlays(canvas, params)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out_path, format="JPEG", quality=92)
    return out_path
