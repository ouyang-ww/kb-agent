"""同步通道预留。

只提供许可、稳定 id 和本机路径映射。不实现移动端，不打开网络，
也不把原件全文写入应用库。
"""

from __future__ import annotations

from pathlib import Path

from ..errors import KBError, NotFoundError
from .ids import KIND_RECORD, KIND_VAULT, parse_stable_id
from .paths import join_under, normalize_relative, normalize_root, relative_under
from .permissions import allows, normalize_grant_name
from .store import SyncStore

# 预留没有传输实现，也没有移动端客户端。原件全文不因同步写入应用库。
TRANSPORT_IMPLEMENTED = False
MOBILE_CLIENT = False
COPIES_ORIGINAL = False


class SyncReservation:
    """将来同步通道要用的本地登记。现在不传输任何内容。"""

    def __init__(self, db_path: str | Path) -> None:
        self.store = SyncStore(db_path)

    def device_id(self) -> str:
        return self.store.device_id()

    def grants(self) -> dict[str, bool]:
        return self.store.grants()

    def grant(self, name: str) -> dict[str, bool]:
        self.store.set_grant(normalize_grant_name(name), True)
        return self.grants()

    def revoke(self, name: str) -> dict[str, bool]:
        self.store.set_grant(normalize_grant_name(name), False)
        return self.grants()

    def allows(self, channel: str, kind: str) -> bool:
        return allows(self.grants(), channel, kind)

    def describe(self) -> dict:
        """给本机状态用。不含绝对路径，也不表示已经在传输。"""
        return {
            "device_id": self.device_id(),
            "transport": TRANSPORT_IMPLEMENTED,
            "mobile_client": MOBILE_CLIENT,
            "copies_original": COPIES_ORIGINAL,
            "grants": self.grants(),
            "mapped": len(self.store.mapped_ids()),
        }

    def bind_path(self, path: str | Path) -> str:
        """把本机文件夹记到一个新的或已有的稳定 id 上。不读取其中的文件。"""
        root = normalize_root(path)
        return self.store.bind_root(str(root))

    def rebind_path(self, stable_id: str, path: str | Path) -> str:
        """同一稳定 id 改指向这台设备上的另一个文件夹。"""
        vault_id = parse_stable_id(stable_id, KIND_VAULT)
        root = normalize_root(path)
        self.store.rebind_root(vault_id, str(root))
        return vault_id

    def local_path(self, stable_id: str) -> Path:
        vault_id = parse_stable_id(stable_id, KIND_VAULT)
        local = self.store.path_for(vault_id)
        if local is None:
            raise KBError("这个稳定 id 在本机还没有路径映射")
        return Path(local)

    def mapped_ids(self) -> list[str]:
        return self.store.mapped_ids()

    def resolve(self, stable_id: str, relative: str) -> Path:
        root = self.local_path(stable_id)
        return join_under(root, relative)

    def locate(self, path: str | Path) -> tuple[str, str] | None:
        """本机路径落在哪个映射下。返回 (稳定 id, 相对位置)。映射外返回 None。"""
        try:
            candidate = Path(path).expanduser().resolve()
        except (FileNotFoundError, OSError):
            return None
        best: tuple[int, str, str] | None = None
        for stable_id, root_text in self.store.roots():
            relative = relative_under(Path(root_text), candidate)
            if relative is None:
                continue
            rank = len(Path(root_text).parts)
            if best is None or rank > best[0]:
                best = (rank, stable_id, relative)
        if best is None:
            return None
        return best[1], best[2]

    def register_record(
        self,
        vault_id: str,
        relative: str,
        local_doc_id: str = "",
    ) -> str:
        """给文件夹内的一条记录分配稳定 id。不读取文件。"""
        vault = parse_stable_id(vault_id, KIND_VAULT)
        root = self.store.path_for(vault)
        if root is None:
            raise KBError("这个稳定 id 在本机还没有路径映射")
        rel = normalize_relative(relative)
        join_under(Path(root), rel)
        return self.store.upsert_record(vault, rel, local_doc_id)

    def relocate_record(self, stable_id: str, relative: str) -> str:
        """相对路径可以变，稳定 id 不变。"""
        record_id = parse_stable_id(stable_id, KIND_RECORD)
        row = self.store.get_record(record_id)
        if row is None:
            raise NotFoundError("没有这条稳定 id")
        rel = normalize_relative(relative)
        root = self.store.path_for(row["vault_id"])
        if root is not None:
            join_under(Path(root), rel)
        self.store.relocate_record(record_id, rel)
        return record_id

    def record(self, stable_id: str) -> dict[str, str]:
        record_id = parse_stable_id(stable_id, KIND_RECORD)
        row = self.store.get_record(record_id)
        if row is None:
            raise NotFoundError("没有这条稳定 id")
        return row

    def close(self) -> None:
        self.store.close()
