"""时效：默认检索排除已过期文档，过期来源被显式使用时标注时间。

``valid_until`` 是「到该日期后视为过期」。比较用 UTC 日期。当天仍有效，
次日（``valid_until < today``）起默认检索不再返回。
这只钟只服务检索过期，保持 UTC。学习队列的日历日不走这里。

向量 ``where`` 无法表达 NULL。未设截止日期的切块写成 ``9999-12-31``，
这样 ``valid_until >= today`` 能同时放行未过期和未设期限的切块。
缺这个字段的旧切块不会被这道 ``where`` 命中，需要 ``kb governance backfill``。
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from ..errors import KBError

# 未设截止日期。必须大于任何真实日期，并且能放进 Chroma 的数值比较。
# 文档行仍存 YYYY-MM-DD 文本；切块元数据写成整数 YYYYMMDD，因为 Chroma 的
# ``$gte`` 不接受字符串。
OPEN_ENDED = "9999-12-31"
OPEN_ENDED_ORD = 99991231

_DAY = re.compile(r"^(\d{4}-\d{2}-\d{2})")


def today_utc() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def normalize_day(value: object) -> str | None:
    """取出 YYYY-MM-DD。空串和无法识别的值视为没有截止日期。"""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text == OPEN_ENDED:
        return None
    match = _DAY.match(text)
    if match is None:
        return None
    return match.group(1)


def resolve_as_of(as_of: str | None) -> str | None:
    """历史查询的比较日。空表示用今天。格式不对就拒绝，避免静默搜错年份。"""
    if as_of is None or str(as_of).strip() == "":
        return None
    day = normalize_day(as_of)
    if day is None:
        raise KBError("as_of 必须是 YYYY-MM-DD")
    return day


def is_expired(valid_until: object, as_of: str | None = None) -> bool:
    """``valid_until`` 早于比较日则过期。当天仍有效。未设置不算过期。"""
    until = normalize_day(valid_until)
    if until is None:
        return False
    day = as_of or today_utc()
    return until < day


def _as_number(value: object) -> object:
    if isinstance(value, bool) or isinstance(value, (str, bytes)):
        return value
    item = getattr(value, "item", None)
    if callable(item):
        try:
            return item()
        except Exception:
            return value
    return value


def day_number(value: object) -> int | None:
    """YYYY-MM-DD 或 YYYYMMDD → 整数。开放哨兵和空值返回 None。"""
    value = _as_number(value)
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = int(value)
        if number >= OPEN_ENDED_ORD or number <= 0:
            return None
        return number
    day = normalize_day(value)
    if day is None:
        return None
    return int(day.replace("-", ""))


def chunk_has_valid_until(value: object) -> bool:
    """切块是否写了截止日期。缺字段、空串都不算；开放哨兵算。"""
    value = _as_number(value)
    if value is None or isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return int(value) > 0
    text = str(value).strip()
    if not text:
        return False
    if text == OPEN_ENDED:
        return True
    return normalize_day(text) is not None


def parse_explicit_day(value: object, field: str) -> str | None:
    """入库用。空和开放哨兵表示未设置。非空但不是合法日期则拒绝。"""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text == OPEN_ENDED:
        return None
    day = normalize_day(text)
    if day is None:
        raise KBError(f"{field} 无法解析为 YYYY-MM-DD：{text}")
    try:
        datetime.strptime(day, "%Y-%m-%d")
    except ValueError as exc:
        raise KBError(f"{field} 无法解析为 YYYY-MM-DD：{text}") from exc
    return day


def expiry_is_enforced(where) -> bool:
    """合并后的过滤是否要求 valid_until 不早于某个正的比较日。

    和 scope 的允许列表一样：这道条件不在 AND 链上，就不能做默认检索。
    """
    from ..storage.filters import Filter, normalize_filter

    filt = where if isinstance(where, Filter) else normalize_filter(where)
    if not filt:
        return False
    for cond in _and_conditions(filt):
        if cond.field != "valid_until" or cond.op not in ("gte", "gt"):
            continue
        try:
            if float(cond.value) > 0:
                return True
        except (TypeError, ValueError):
            continue
    return False


def _and_conditions(filt) -> list:
    found = list(filt.conditions)
    for sub in getattr(filt, "conjuncts", ()) or ():
        found.extend(_and_conditions(sub))
    return found


def chunk_valid_until(value: object) -> int:
    """写入切块的截止日期。空值用开放哨兵，方便向量 where 做数值比较。"""
    return day_number(value) or OPEN_ENDED_ORD


def display_day(value: object) -> str:
    """切块上的整数日期还原成 YYYY-MM-DD。开放哨兵还原成空串。"""
    value = _as_number(value)
    if isinstance(value, bool):
        return ""
    if isinstance(value, (int, float)):
        number = int(value)
        if number >= OPEN_ENDED_ORD or number <= 0:
            return ""
        text = f"{number:08d}"
        return f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    return normalize_day(value) or ""


def earlier_deadline(*values: object) -> str:
    """几个截止日期里更早的那个。都没设置则返回空串。"""
    days = [day for day in (normalize_day(value) for value in values) if day]
    if not days:
        return ""
    return min(days)


def expiry_note(valid_until: object, last_verified: object = None) -> str:
    when = normalize_day(last_verified) or normalize_day(valid_until) or str(valid_until or "")
    return f"注：该来源最后标注时间为 {when}，可能已过时。"


def validity_filter(as_of: str | None = None):
    """默认检索的时效条件：``valid_until >= 比较日``，比较日是整数 YYYYMMDD。

    与 scope/status 的允许列表叠在一起。``include_expired`` 时不要加这道条件。
    切块上的 ``valid_until`` 必须是整数；未设置写成 ``99991231``。
    """
    from ..storage.filters import gte

    day = as_of or today_utc()
    return gte("valid_until", day_number(day) or 0)
