"""Shared scheduling rules for the platform's native scheduled publishing."""
import time
from datetime import datetime
from zoneinfo import ZoneInfo

MIN_SCHEDULE_LEAD_SECONDS = 60 * 60
PLAN_LEAD_MINUTES = 90  # Leave time for uploading and the serial queue.


def schedule_epoch(payload):
    raw = str(payload.get("schedule_time") or "").strip()
    if not raw:
        return None
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None and payload.get("timezone"):
        dt = dt.replace(tzinfo=ZoneInfo(payload["timezone"]))
    epoch = dt.timestamp()
    if payload.get("schedule_epoch") is not None and abs(float(payload["schedule_epoch"]) - epoch) >= 60:
        raise ValueError("排期时间与时区记录不一致，请重新排期")
    return epoch


def require_schedule_lead(payload, now=None):
    if payload.get("publish_mode") == "immediate":
        return
    epoch = schedule_epoch(payload)
    if epoch is None:
        if payload.get("publish_mode") == "scheduled":
            raise ValueError("缺少定时发布时间，请重新排期")
        return
    if epoch < (time.time() if now is None else now) + MIN_SCHEDULE_LEAD_SECONDS:
        raise ValueError("小红书定时发布需要至少提前 1 小时；时间已过或不足，请重新排期")
