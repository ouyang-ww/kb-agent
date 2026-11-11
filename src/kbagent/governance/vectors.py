"""把文档的 scope / status 写回已有切块，不重新嵌入。

hide / unhide / 回填走这里。向量查询的 where 只认切块上的元数据，
文档行是第二道，两边要一起改。
"""

from __future__ import annotations

from typing import Any

from ..errors import KBError
from ..storage.filters import eq


def _plain(meta: dict[str, Any] | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in (meta or {}).items():
        if isinstance(value, bool) or isinstance(value, (str, int, float)):
            out[key] = value
        elif hasattr(value, "item"):
            try:
                out[key] = value.item()
            except Exception:
                continue
    return out


def sync_chunk_governance(
    vector_store,
    collection: str,
    doc_id: str,
    *,
    status: str | None = None,
    scope: str | None = None,
    valid_until: int | str | None = None,
    fill_open_validity: bool = False,
) -> int:
    """按 doc_id 更新切块的 status / scope / valid_until。没变化的切块不写。

    ``fill_open_validity`` 只在切块还没有 ``valid_until`` 时写成开放哨兵，
    避免把已经写过的截止日期盖掉。返回实际更新的条数。向量库没有
    ``update_metadata`` 时抛错，不能假装已经同步。
    """
    from .validity import OPEN_ENDED_ORD

    if not doc_id or (
        status is None and scope is None and valid_until is None and not fill_open_validity
    ):
        return 0
    rows = vector_store.get(collection, where=eq("doc_id", doc_id))
    ids: list[str] = []
    metas: list[dict[str, Any]] = []
    for row in rows:
        meta = _plain(row.metadata)
        meta["doc_id"] = doc_id
        changed = False
        if status is not None and meta.get("status") != status:
            meta["status"] = status
            changed = True
        if scope is not None and meta.get("scope") != scope:
            meta["scope"] = scope
            changed = True
        if valid_until and meta.get("valid_until") != valid_until:
            meta["valid_until"] = valid_until
            changed = True
        elif fill_open_validity and not meta.get("valid_until"):
            meta["valid_until"] = OPEN_ENDED_ORD
            changed = True
        if not changed:
            continue
        ids.append(row.id)
        metas.append(meta)
    if not ids:
        return 0
    updater = getattr(vector_store, "update_metadata", None)
    if not callable(updater):
        raise KBError("向量库不支持更新元数据，无法同步 scope/status")
    updater(collection, ids, metas)
    return len(ids)
