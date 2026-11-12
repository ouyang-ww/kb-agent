"""学习队列的日历日。固定 Asia/Shanghai。

不跟进程本地时区。P2 检索过期用的是 UTC 日（``governance.validity.today_utc``），
这刀不改那只钟。上海 0:00 到 8:00 之间，UTC 日还停在前一天。
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")


def clock() -> datetime:
    return datetime.now(timezone.utc)


def shanghai_today(now: datetime | None = None) -> date:
    moment = clock() if now is None else now
    if moment.tzinfo is None:
        raise ValueError("学习日期必须带时区")
    return moment.astimezone(SHANGHAI).date()
