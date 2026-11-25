"""本机路径映射的规则。绝对路径只属于这一台设备。"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from ..errors import KBError


def normalize_root(path: str | Path) -> Path:
    """映射目标必须是这台设备上已经存在的文件夹。"""
    raw = str(path or "").strip()
    if not raw:
        raise KBError("本机路径不能为空")
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    try:
        resolved = candidate.resolve(strict=True)
    except (FileNotFoundError, OSError) as exc:
        raise KBError("本机路径不存在，不能映射") from exc
    if not resolved.is_dir():
        raise KBError("本机路径映射只接受文件夹")
    return resolved


def normalize_relative(relative: str) -> str:
    """记录在文件夹内的位置。拒绝绝对路径和越出根目录的 ..。"""
    text = (relative or "").strip().replace("\\", "/")
    if not text:
        raise KBError("记录位置必须是相对路径")
    if text.startswith(("/", "~")):
        raise KBError("记录位置必须是相对路径，不能是本机绝对路径")
    if len(text) >= 2 and text[1] == ":" and text[0].isalpha():
        raise KBError("记录位置必须是相对路径，不能是本机绝对路径")
    parts = [part for part in text.split("/") if part]
    if not parts or any(part in {".", ".."} for part in parts):
        raise KBError("记录位置不能包含 . 或 ..")
    return "/".join(parts)


def _require_under(path: Path, base: Path) -> None:
    try:
        path.relative_to(base)
    except ValueError:
        raise KBError("路径越出本机映射") from None


def join_under(root: Path, relative: str) -> Path:
    """把相对位置接到本机根上。符号链接若指向根以外，拒绝。

    逐段检查已经存在的目录和符号链接。末端文件还不存在时，
    中间目录若是指向根以外的符号链接，不能因为叶子缺失就放过。
    """
    rel = normalize_relative(relative)
    base = Path(root).resolve()
    current = base
    parts = PurePosixPath(rel).parts
    for index, part in enumerate(parts):
        nxt = current.joinpath(part)
        if nxt.exists() or nxt.is_symlink():
            try:
                real = nxt.resolve()
            except (OSError, RuntimeError):
                raise KBError("路径越出本机映射") from None
            _require_under(real, base)
            current = real
            continue
        rest = current.joinpath(*parts[index:])
        _require_under(rest, base)
        return rest
    return current


def relative_under(root: Path, local_path: Path) -> str | None:
    """local_path 落在 root 下时返回相对位置。根自身是空串。落在外面返回 None。"""
    base = Path(root).resolve()
    try:
        candidate = Path(local_path).expanduser().resolve()
    except (FileNotFoundError, OSError):
        return None
    try:
        rel = candidate.relative_to(base)
    except ValueError:
        return None
    if rel == Path("."):
        return ""
    return rel.as_posix()
