"""用已绑定的源路径打开原文。

优先 Obsidian，否则交给系统默认应用。调用方只提供文档 id。
这里不接受路径、不列目录、不读正文，也不执行 shell。
打开不会把原文写回应用库，见 ADR 0002。
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from .errors import KBError, NotFoundError
from .models import SOURCE_BOUND

NOT_VISIBLE = "无法打开原文：文档不存在或当前范围不可见"
NOT_BOUND = "无法打开原文：该文档没有绑定的外部原件"
NO_PATH = "无法打开原文：绑定文档没有可用的源路径"
MISSING_FILE = "无法打开原文：绑定的源文件不存在"
OPEN_FAILED = "无法打开原文：系统未能启动外部应用"
NO_OPENER = "无法打开原文：当前系统没有可用的打开方式"

_WHICH = ("obsidian", "Obsidian", "Obsidian.exe")


@dataclass(frozen=True)
class OpenResult:
    """打开结果。不包含源路径，调用方不需要绝对路径。"""

    doc_id: str
    title: str
    opener: str  # obsidian | system


@dataclass(frozen=True)
class Spawn:
    """一次外部打开。``argv`` 为空时用操作系统的 startfile，不经过 shell。"""

    argv: tuple[str, ...] | None
    target: str


class OriginalLaunch:
    """测试替换这个对象。生产环境用 :class:`SystemOpenLaunch`。"""

    def obsidian_installed(self) -> bool:
        raise NotImplementedError

    def open_obsidian(self, path: Path) -> None:
        raise NotImplementedError

    def open_system(self, path: Path) -> None:
        raise NotImplementedError


def obsidian_open_uri(path: Path) -> str:
    """Obsidian 的打开协议。路径整体编码，避免空格和反斜杠被拆开。"""
    return "obsidian://open?path=" + quote(str(path), safe="")


def spawn_for(kind: str, target: str, system: str) -> Spawn:
    """按系统选出打开命令。不拼接 shell 字符串。"""
    if kind == "obsidian":
        uri = obsidian_open_uri(Path(target))
        if system == "Windows":
            return Spawn(None, uri)
        if system == "Darwin":
            return Spawn(("open", uri), uri)
        if system == "Linux":
            return Spawn(("xdg-open", uri), uri)
        raise KBError(NO_OPENER)
    if kind == "system":
        if system == "Windows":
            return Spawn(None, target)
        if system == "Darwin":
            return Spawn(("open", target), target)
        if system == "Linux":
            return Spawn(("xdg-open", target), target)
        raise KBError(NO_OPENER)
    raise KBError(NO_OPENER)


def obsidian_candidates(
    system: str,
    environ: Mapping[str, str],
    home: Path,
) -> list[Path]:
    """只检查固定安装位置，不扫描磁盘。"""
    found: list[Path] = []
    if system == "Darwin":
        found.append(Path("/Applications/Obsidian.app"))
        found.append(home / "Applications" / "Obsidian.app")
    elif system == "Windows":
        local = environ.get("LOCALAPPDATA", "")
        program = environ.get("ProgramFiles", "")
        program_x86 = environ.get("ProgramFiles(x86)", "")
        for root in (local, program, program_x86):
            if not root:
                continue
            base = Path(root)
            found.append(base / "Obsidian" / "Obsidian.exe")
            found.append(base / "Programs" / "Obsidian" / "Obsidian.exe")
    else:
        found.extend(
            [
                Path("/usr/bin/obsidian"),
                Path("/usr/local/bin/obsidian"),
                Path("/snap/bin/obsidian"),
                Path("/opt/Obsidian/obsidian"),
                home / ".local" / "bin" / "obsidian",
                home
                / ".local"
                / "share"
                / "flatpak"
                / "exports"
                / "bin"
                / "md.obsidian.Obsidian",
                Path("/var/lib/flatpak/exports/bin/md.obsidian.Obsidian"),
            ]
        )
    return found


def obsidian_installed(
    *,
    system: str | None = None,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
    which: Callable[[str], str | None] = shutil.which,
    exists: Callable[[Path], bool] | None = None,
) -> bool:
    """Obsidian 是否已安装。只查 PATH 和固定位置，不启动应用。

    macOS 上 ``Obsidian.app`` 是目录，所以用存在性而不是 ``is_file``。
    """
    name = system or platform.system()
    env = os.environ if environ is None else environ
    root = Path.home() if home is None else home
    present = Path.exists if exists is None else exists
    for command in _WHICH:
        if which(command):
            return True
    for candidate in obsidian_candidates(name, env, root):
        try:
            if present(candidate):
                return True
        except OSError:
            continue
    return False


def _startfile(target: str) -> None:
    startfile = getattr(os, "startfile", None)
    if startfile is None:
        raise KBError(NO_OPENER)
    try:
        startfile(target)
    except OSError:
        raise KBError(OPEN_FAILED) from None


def _run(argv: list[str]) -> None:
    try:
        subprocess.run(argv, check=True)
    except (OSError, subprocess.CalledProcessError):
        raise KBError(OPEN_FAILED) from None


class SystemOpenLaunch(OriginalLaunch):
    """本机打开：Obsidian 协议，或系统默认应用。"""

    def obsidian_installed(self) -> bool:
        return obsidian_installed()

    def open_obsidian(self, path: Path) -> None:
        self._execute(spawn_for("obsidian", str(path), platform.system()))

    def open_system(self, path: Path) -> None:
        self._execute(spawn_for("system", str(path), platform.system()))

    def _execute(self, spawn: Spawn) -> None:
        if spawn.argv is None:
            _startfile(spawn.target)
            return
        program = spawn.argv[0]
        if shutil.which(program) is None:
            raise KBError(NO_OPENER)
        _run(list(spawn.argv))


def _usable_file(stored: str) -> Path:
    """已保存的路径必须本身就是现有文件。不向上查找，也不挑目录里的别的文件。"""
    if not stored or "\x00" in stored:
        raise KBError(NO_PATH)
    candidate = Path(stored)
    try:
        is_file = candidate.is_file()
    except OSError:
        raise KBError(MISSING_FILE) from None
    if not is_file:
        raise KBError(MISSING_FILE)
    try:
        absolute = candidate.resolve()
        if not absolute.is_file():
            raise KBError(MISSING_FILE)
    except OSError:
        raise KBError(MISSING_FILE) from None
    return absolute


def open_bound_original(
    kb,
    doc_id: str,
    *,
    launch: OriginalLaunch | None = None,
) -> OpenResult:
    """打开这篇文档入库时绑定的源文件。

    必须和默认检索同一道门：``_default_retrievable`` 为假（隐藏、private、
    secret、已过期，或不在默认 scope 允许列表里）就失败。没有 include-expired
    之类的旁路。不读取文件内容，也不写回 ``documents.sqlite3``。
    """
    key = (doc_id or "").strip()
    if not key:
        raise NotFoundError(NOT_VISIBLE)
    doc = kb.get_document(key, include_hidden=False)
    # get_document 只排除隐藏 / private / secret。过期和允许列表之外的 scope
    # 仍能被读到，但默认 search / ask 不会返回，这里同样不能打开。
    if doc is None or not kb._default_retrievable(key):
        raise NotFoundError(NOT_VISIBLE)
    if doc.source_kind != SOURCE_BOUND:
        raise KBError(NOT_BOUND)
    absolute = _usable_file((doc.source_path or "").strip())
    opener = launch or SystemOpenLaunch()
    if opener.obsidian_installed():
        try:
            opener.open_obsidian(absolute)
            which = "obsidian"
        except KBError:
            # Obsidian 装了但没能启动时，改用系统默认应用。错误信息里不带路径。
            opener.open_system(absolute)
            which = "system"
    else:
        opener.open_system(absolute)
        which = "system"
    return OpenResult(doc_id=doc.doc_id, title=doc.title, opener=which)
