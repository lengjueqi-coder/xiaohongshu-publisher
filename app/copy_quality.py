"""Conservative local checks; these do not judge factual or artistic quality."""
import json
import re
from difflib import SequenceMatcher
from pathlib import Path


def material_notes(folder):
    path = Path(folder) / "发布说明.txt"
    if not path.exists():
        return ""
    if not path.is_file() or path.stat().st_size > 24000:
        raise ValueError("发布说明.txt 必须是小于 24KB 的 UTF-8 文本")
    return path.read_text("utf-8-sig").strip()[:6000]


def recent_copies(app, profile):
    with app.db_connection() as db:
        rows = db.execute("""SELECT title,content FROM publish_history WHERE profile_dir=?
            AND status IN ('accepted','scheduled','reviewing','published','unknown') ORDER BY id DESC LIMIT 30""", (profile,)).fetchall()
        jobs = db.execute("""SELECT payload FROM publish_jobs WHERE profile_dir=?
            AND status NOT IN ('failed','cancelled') ORDER BY id DESC LIMIT 30""", (profile,)).fetchall()
    result = [{"title": r[0], "content": r[1]} for r in rows]
    result += [json.loads(r[0]) for r in jobs]
    return [{"title": str(r.get("title") or ""), "content": str(r.get("content") or "")} for r in result]


def ensure_distinct(copy, recent):
    compact = lambda value: re.sub(r"[\W_]+", "", str(value))
    title, body = compact(copy["title"]), compact(copy["content"])
    for previous in recent:
        old_title, old_body = compact(previous["title"]), compact(previous["content"])
        if title == old_title or (body and old_body and SequenceMatcher(None, body, old_body, autojunk=False).ratio() >= .88):
            raise ValueError("文案与该账号近期内容过于相似，请更换角度或补充发布说明.txt 后重新生成")
