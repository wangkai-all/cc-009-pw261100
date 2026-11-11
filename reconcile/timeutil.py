"""时间工具：结算日一律按北京时间（UTC+8）划分。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

BEIJING_TZ = timezone(timedelta(hours=8))


def parse_beijing(raw: str) -> datetime:
    """解析时间字符串并转为带时区的北京时间。

    支持：
      - 朴素时间 "2026-11-09 10:00:00"（按北京时间解释）
      - ISO8601 带偏移 "2026-11-09T10:00:00+08:00" / "...Z"
    """
    text = str(raw).strip()
    if not text:
        raise ValueError("时间为空")
    normalized = text.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        dt = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=BEIJING_TZ)
    return dt.astimezone(BEIJING_TZ)


def beijing_iso(raw: str) -> str:
    return parse_beijing(raw).isoformat()


def beijing_date(raw: str) -> str:
    """返回北京时间日期字符串 YYYY-MM-DD。"""
    return parse_beijing(raw).date().isoformat()
