"""本机 MCP（search / ask / study）和按绑定路径打开原文。"""

from __future__ import annotations

import inspect
import io
import json
import os
import platform
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from kbagent.cli import app
from kbagent.errors import KBError
from kbagent.mcp.server import McpServer, dispatch_line, redact_external, serve_stdio
from kbagent.models import SOURCE_BOUND, SOURCE_DIVERGED, Document
from kbagent.open_original import (
    MISSING_FILE,
    NO_PATH,
    NOT_BOUND,
    NOT_VISIBLE,
    NO_OPENER,
    OPEN_FAILED,
    SystemOpenLaunch,
    obsidian_installed,
    obsidian_open_uri,
    open_bound_original,
    spawn_for,
)
from kbagent.study.cards import HARDCODED_CARDS

_TAIL = "TAILMARKER-只应留在磁盘上-" + ("泽" * 40)


class RecordingLaunch:
    def __init__(self, obsidian: bool = True) -> None:
        self._obsidian = obsidian
        self.calls: list[tuple[str, Path]] = []

    def obsidian_installed(self) -> bool:
        return self._obsidian

    def open_obsidian(self, path: Path) -> None:
        self.calls.append(("obsidian", Path(path)))

    def open_system(self, path: Path) -> None:
        self.calls.append(("system", Path(path)))


def _rpc(server: McpServer, method: str, params: dict | None = None, msg_id: int = 1):
    message: dict = {"jsonrpc": "2.0", "id": msg_id, "method": method}
    if params is not None:
        message["params"] = params
    response = server.handle(message)
    assert response is not None
    assert response["id"] == msg_id
    return response


def _ok(response: dict) -> dict:
    assert "error" not in response, response
    result = response["result"]
    assert result["isError"] is False, result
    return json.loads(result["content"][0]["text"])


def _err(response: dict) -> str:
    assert "error" not in response, response
    result = response["result"]
    assert result["isError"] is True, result
    return result["content"][0]["text"]


def _tools(server: McpServer) -> dict[str, dict]:
    listed = _rpc(server, "tools/list", {})
    return {tool["name"]: tool for tool in listed["result"]["tools"]}


def _call(server: McpServer, name: str, arguments: dict | None = None, msg_id: int = 1):
    return _rpc(
        server,
        "tools/call",
        {"name": name, "arguments": arguments or {}},
        msg_id,
    )


def _row(kb, doc_id: str):
    conn = sqlite3.connect(kb.settings.documents_db)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT full_text, source_kind, source_path FROM documents WHERE doc_id = ?",
        (doc_id,),
    ).fetchone()
    conn.close()
    return row


def _sqlite_bytes(kb) -> bytes:
    path = Path(kb.settings.documents_db)
    blob = path.read_bytes()
    for suffix in ("-wal", "-shm"):
        extra = Path(str(path) + suffix)
        if extra.exists():
            blob += extra.read_bytes()
    return blob


def _long_note(path: Path) -> str:
    body = ("绑定原文开头。" * 40) + _TAIL
    path.write_text(body, encoding="utf-8")
    return body


def _ingest(kb, path: Path, **kwargs) -> str:
    report = kb.ingest(path, **kwargs)
    assert report.added, report.failed
    return report.added[0]


def _seed_visible_chunk(kb, doc_id: str) -> None:
    """给直接写入的文档补一条默认可检索的切块，后面的路径检查才走得到。"""
    from helpers import FakeEmbedder

    kb._vector_store.add(
        kb.settings.default_collection,
        ids=[f"{doc_id}#visible"],
        embeddings=[FakeEmbedder().embed_query(doc_id)],
        documents=["visible-gate"],
        metadatas=[
            {
                "doc_id": doc_id,
                "status": "active",
                "scope": "default",
                "valid_until": 99991231,
            }
        ],
    )


# ---------- 打开原文：命令选择与探测，不启动 GUI ----------


def test_obsidian_uri_encodes_spaces_and_backslashes():
    uri = obsidian_open_uri(Path("/vault/my notes/a.md"))
    assert uri.startswith("obsidian://open?path=")
    assert " " not in uri
    assert "my%20notes" in uri
    encoded = obsidian_open_uri(Path("C:/Notes/a.md"))
    assert "%3A" in encoded or "C:" not in encoded.split("path=", 1)[-1]


def test_spawn_commands_are_not_a_shell():
    windows_file = spawn_for("system", r"C:\Notes\a.md", "Windows")
    assert windows_file.argv is None
    assert windows_file.target == r"C:\Notes\a.md"
    mac = spawn_for("system", "/vault/a.md", "Darwin")
    assert mac.argv == ("open", "/vault/a.md")
    linux = spawn_for("obsidian", "/vault/my notes/a.md", "Linux")
    assert linux.argv is not None
    assert linux.argv[0] == "xdg-open"
    assert linux.argv[1].startswith("obsidian://open?path=")
    assert " " not in linux.argv[1]
    with pytest.raises(KBError, match=NO_OPENER):
        spawn_for("system", "/vault/a.md", "Plan9")


def test_obsidian_detection_uses_fixed_locations_and_does_not_spawn(monkeypatch):
    def fail(*_args, **_kwargs):
        raise AssertionError("探测 Obsidian 不应启动进程")

    monkeypatch.setattr(subprocess, "run", fail)
    monkeypatch.setattr(os, "startfile", fail, raising=False)
    assert obsidian_installed() in (True, False)

    assert obsidian_installed(
        system="Linux",
        environ={},
        home=Path("/tmp/nobody"),
        which=lambda _name: None,
        exists=lambda _path: False,
    ) is False
    assert obsidian_installed(
        system="Linux",
        which=lambda name: "/usr/bin/obsidian" if name == "obsidian" else None,
        exists=lambda _path: False,
    ) is True
    bundle = Path("/Applications/Obsidian.app")
    assert obsidian_installed(
        system="Darwin",
        environ={},
        home=Path("/Users/someone"),
        which=lambda _name: None,
        exists=lambda path: path == bundle,
    ) is True
    local = r"C:\Users\a\AppData\Local"
    exe = Path(local) / "Obsidian" / "Obsidian.exe"
    assert obsidian_installed(
        system="Windows",
        environ={"LOCALAPPDATA": local},
        home=Path(r"C:\Users\a"),
        which=lambda _name: None,
        exists=lambda path: path == exe,
    ) is True


def test_system_launch_prefers_platform_opener_without_shell(monkeypatch, tmp_path):
    note = tmp_path / "platform-note.md"
    note.write_text("只用于构造路径。", encoding="utf-8")
    calls: list[list[str]] = []

    def fake_run(argv, check):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(shutil, "which", lambda cmd: f"/usr/bin/{cmd}")

    monkeypatch.setattr(platform, "system", lambda: "Linux")
    SystemOpenLaunch().open_system(note)
    assert calls == [["xdg-open", str(note)]]

    calls.clear()
    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    SystemOpenLaunch().open_system(note)
    assert calls == [["open", str(note)]]

    started: list[str] = []
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    monkeypatch.setattr(
        "kbagent.open_original._startfile", lambda target: started.append(target)
    )
    SystemOpenLaunch().open_obsidian(note)
    assert started and started[0].startswith("obsidian://open?path=")
    assert str(note) not in started[0]
    assert calls == [["open", str(note)]]


def test_missing_opener_and_launch_failure_hide_the_path(monkeypatch, tmp_path):
    note = tmp_path / "hidden-launch-target.md"
    note.write_text("不会被打开。", encoding="utf-8")
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(shutil, "which", lambda _cmd: None)
    with pytest.raises(KBError, match=NO_OPENER) as missing:
        SystemOpenLaunch().open_system(note)
    assert str(note) not in str(missing.value)
    assert note.name not in str(missing.value)

    def boom(argv, check):
        raise subprocess.CalledProcessError(1, argv)

    monkeypatch.setattr(shutil, "which", lambda _cmd: "/usr/bin/xdg-open")
    monkeypatch.setattr(subprocess, "run", boom)
    with pytest.raises(KBError, match=OPEN_FAILED) as failed:
        SystemOpenLaunch().open_system(note)
    assert str(note) not in str(failed.value)


def test_open_uses_bound_path_and_prefers_obsidian_then_system(kb, tmp_path):
    obsidian_note = tmp_path / "obsidian-bound.md"
    system_note = tmp_path / "system-bound.md"
    _long_note(obsidian_note)
    system_note.write_text("交给系统默认应用的原文。", encoding="utf-8")
    first = _ingest(kb, obsidian_note)
    second = _ingest(kb, system_note)
    prefer = RecordingLaunch(True)
    opened = open_bound_original(kb, first, launch=prefer)
    assert opened.opener == "obsidian"
    assert opened.doc_id == first
    assert prefer.calls == [("obsidian", obsidian_note.resolve())]

    fallback = RecordingLaunch(False)
    opened = open_bound_original(kb, second, launch=fallback)
    assert opened.opener == "system"
    assert fallback.calls == [("system", system_note.resolve())]
    assert all(call[0] == "system" for call in fallback.calls)


def test_open_fail_closed_for_hidden_unbound_and_missing(kb, tmp_path):
    secret = "隐蔽正文不应出现在错误里-UNICORN"
    hidden_note = tmp_path / "hidden-source.md"
    hidden_note.write_text(secret, encoding="utf-8")
    hidden_id = _ingest(kb, hidden_note)
    assert kb.hide_document(hidden_id)
    with pytest.raises(KBError, match=NOT_VISIBLE) as hidden_exc:
        open_bound_original(kb, hidden_id, launch=RecordingLaunch())
    assert secret not in str(hidden_exc.value)
    assert str(hidden_note) not in str(hidden_exc.value)

    private_note = tmp_path / "private-source.md"
    private_note.write_text("私人范围的原文-" + secret, encoding="utf-8")
    private_id = _ingest(kb, private_note, scope="private")
    with pytest.raises(KBError, match=NOT_VISIBLE) as private_exc:
        open_bound_original(kb, private_id, launch=RecordingLaunch())
    assert str(private_note) not in str(private_exc.value)
    assert secret not in str(private_exc.value)

    with pytest.raises(KBError, match=NOT_VISIBLE):
        open_bound_original(kb, "missing-doc", launch=RecordingLaunch())

    inline_body = "INLINE-BODY-没有外部原件"
    inline_id = kb.ingest_text(inline_body, title="inline-note")
    with pytest.raises(KBError, match=NOT_BOUND) as inline_exc:
        open_bound_original(kb, inline_id, launch=RecordingLaunch())
    assert inline_body not in str(inline_exc.value)

    gone = tmp_path / "gone.md"
    _long_note(gone)
    gone_id = _ingest(kb, gone)
    gone.unlink()
    with pytest.raises(KBError, match=MISSING_FILE) as gone_exc:
        open_bound_original(kb, gone_id, launch=RecordingLaunch())
    assert str(gone) not in str(gone_exc.value)
    assert gone.name not in str(gone_exc.value)
    assert _row(kb, gone_id)["full_text"] == ""

    folder = tmp_path / "not-a-file"
    folder.mkdir()
    (folder / "secret-child.md").write_text("子文件不应被挑出来", encoding="utf-8")
    kb._doc_store.upsert(
        Document(
            doc_id="dir-bound",
            title="目录",
            full_text="",
            source_path=str(folder),
            source_kind=SOURCE_BOUND,
        )
    )
    _seed_visible_chunk(kb, "dir-bound")
    assert kb._default_retrievable("dir-bound") is True
    launch = RecordingLaunch()
    with pytest.raises(KBError, match=MISSING_FILE) as dir_exc:
        open_bound_original(kb, "dir-bound", launch=launch)
    assert launch.calls == []
    assert "secret-child" not in str(dir_exc.value)
    assert str(folder) not in str(dir_exc.value)

    drifted = tmp_path / "drifted.md"
    drifted.write_text("哈希已经对不上的文件。", encoding="utf-8")
    kb._doc_store.upsert(
        Document(
            doc_id="drifted",
            title="分叉",
            full_text="",
            source_path=str(drifted),
            source_kind=SOURCE_DIVERGED,
        )
    )
    _seed_visible_chunk(kb, "drifted")
    with pytest.raises(KBError, match=NOT_BOUND):
        open_bound_original(kb, "drifted", launch=RecordingLaunch())
    assert _row(kb, "drifted")["full_text"] == ""

    kb._doc_store.upsert(
        Document(
            doc_id="no-path",
            title="无路径",
            full_text="不应被写回或返回",
            source_path="  ",
            source_kind=SOURCE_BOUND,
        )
    )
    _seed_visible_chunk(kb, "no-path")
    with pytest.raises(KBError, match=NO_PATH) as no_path:
        open_bound_original(kb, "no-path", launch=RecordingLaunch())
    assert "不应被写回或返回" not in str(no_path.value)
    assert _row(kb, "no-path")["full_text"] == ""


def test_open_does_not_write_original_back_into_the_database(kb, tmp_path):
    note = tmp_path / "adr-bound.md"
    _long_note(note)
    doc_id = _ingest(kb, note)
    stored = _row(kb, doc_id)
    assert stored["source_kind"] == SOURCE_BOUND
    assert stored["full_text"] == ""
    assert _TAIL.encode() not in _sqlite_bytes(kb)
    writes: list[str] = []
    original = kb._doc_store.upsert

    def spy(doc):
        writes.append(doc.doc_id)
        return original(doc)

    kb._doc_store.upsert = spy
    source = inspect.getsource(open_bound_original)
    assert "read_text" not in source
    assert "read_bytes" not in source
    assert "upsert" not in source
    opened = open_bound_original(kb, doc_id, launch=RecordingLaunch(True))
    assert opened.opener == "obsidian"
    assert writes == []
    assert _row(kb, doc_id)["full_text"] == ""
    assert _row(kb, doc_id)["source_path"] == str(note)
    assert _TAIL.encode() not in _sqlite_bytes(kb)
    module = Path(inspect.getfile(open_bound_original)).read_text(encoding="utf-8")
    assert "shell=True" not in module
    assert "os.system" not in module


# ---------- MCP ----------


def test_open_rejects_expired_and_scopes_outside_the_default_allowlist(kb, tmp_path):
    expired_note = tmp_path / "expired-bound.md"
    expired_note.write_text(("过期正文不应出现。" * 30) + _TAIL, encoding="utf-8")
    expired_id = _ingest(kb, expired_note)
    meta = kb.get_doc_meta(expired_id)
    assert meta is not None
    meta.valid_until = "2020-01-01"
    kb.governance.upsert_meta(meta)
    assert kb.get_document(expired_id) is not None
    assert kb._default_retrievable(expired_id) is False
    assert all(hit.doc_id != expired_id for hit in kb.search("过期正文不应出现", top_k=5))
    launch = RecordingLaunch()
    with pytest.raises(KBError, match=NOT_VISIBLE) as expired_exc:
        open_bound_original(kb, expired_id, launch=launch)
    assert launch.calls == []
    expired_text = str(expired_exc.value)
    assert str(expired_note) not in expired_text
    assert expired_note.name not in expired_text
    assert "过期正文不应出现" not in expired_text
    assert _TAIL not in expired_text
    assert _row(kb, expired_id)["full_text"] == ""
    assert _TAIL.encode() not in _sqlite_bytes(kb)

    server = McpServer(kb, launch=launch)
    mcp_error = _err(_call(server, "open_original", {"doc_id": expired_id}))
    assert NOT_VISIBLE in mcp_error
    assert str(expired_note) not in mcp_error
    assert "过期正文不应出现" not in mcp_error
    bypass = _err(
        _call(server, "open_original", {"doc_id": expired_id, "include_expired": True})
    )
    assert "include_expired" in bypass
    assert launch.calls == []

    archive_note = tmp_path / "archive-bound.md"
    archive_body = "档案范围的绑定原文不在默认允许列表。"
    archive_note.write_text(archive_body, encoding="utf-8")
    archive_id = _ingest(kb, archive_note, scope="archive")
    assert kb.get_document(archive_id) is not None
    assert kb.get_document(archive_id).scope == "archive"
    assert kb._default_retrievable(archive_id) is False
    assert all(hit.doc_id != archive_id for hit in kb.search(archive_body, top_k=5))
    with pytest.raises(KBError, match=NOT_VISIBLE) as archive_exc:
        open_bound_original(kb, archive_id, launch=launch)
    assert launch.calls == []
    assert str(archive_note) not in str(archive_exc.value)
    assert archive_body not in str(archive_exc.value)
    assert _row(kb, archive_id)["full_text"] == ""

    work_note = tmp_path / "work-bound.md"
    work_note.write_text("工作范围仍在默认允许列表里。", encoding="utf-8")
    work_id = _ingest(kb, work_note, scope="work")
    assert kb._default_retrievable(work_id) is True
    opened = open_bound_original(kb, work_id, launch=RecordingLaunch(True))
    assert opened.doc_id == work_id
    assert opened.opener == "obsidian"


def test_obsidian_launch_failure_falls_back_to_the_system_app(kb, tmp_path):
    note = tmp_path / "obsidian-fails.md"
    note.write_text("Obsidian 打不开时改用系统应用。", encoding="utf-8")
    doc_id = _ingest(kb, note)

    class ObsidianFails(RecordingLaunch):
        def open_obsidian(self, path: Path) -> None:
            super().open_obsidian(path)
            raise KBError(OPEN_FAILED)

    launch = ObsidianFails()
    opened = open_bound_original(kb, doc_id, launch=launch)
    assert opened.opener == "system"
    assert launch.calls == [
        ("obsidian", note.resolve()),
        ("system", note.resolve()),
    ]
    assert _row(kb, doc_id)["full_text"] == ""

    class BothFail(RecordingLaunch):
        def open_obsidian(self, path: Path) -> None:
            raise KBError(OPEN_FAILED)

        def open_system(self, path: Path) -> None:
            raise KBError(OPEN_FAILED)

    with pytest.raises(KBError, match=OPEN_FAILED) as failed:
        open_bound_original(kb, doc_id, launch=BothFail())
    assert str(note) not in str(failed.value)
    assert note.name not in str(failed.value)


def test_redact_external_reuses_absolute_path_rule():
    payload = {
        "source_path": "/tmp/secret/note.md",
        "image_path": "/tmp/secret/a.png",
        "nested": {
            "original_path": "/tmp/secret/orig.md",
            "source_path": "notes/a.md",
        },
        "api_key": "sekret",
        "storage_dir": "/home/x/.kbagent",
        "snippet": "正文里可以有普通文字",
    }
    hidden = redact_external(payload, False)
    assert hidden["source_path"] == ""
    assert hidden["image_path"] == ""
    assert hidden["nested"]["original_path"] == ""
    assert hidden["nested"]["source_path"] == "notes/a.md"
    assert "api_key" not in hidden
    assert "storage_dir" not in hidden
    assert hidden["snippet"] == "正文里可以有普通文字"
    shown = redact_external(payload, True)
    assert shown["source_path"] == "/tmp/secret/note.md"
    assert shown["image_path"] == "/tmp/secret/a.png"


def test_mcp_lists_search_ask_and_open_until_study_is_enabled(kb):
    kb.settings.expose_source_paths = False
    server = McpServer(kb, launch=RecordingLaunch())
    assert not kb.settings.study_db.exists()
    tools = _tools(server)
    assert list(tools) == ["search", "ask", "open_original"]
    assert "study" not in tools
    opened = tools["open_original"]["inputSchema"]
    assert opened["required"] == ["doc_id"]
    assert "path" not in opened["properties"]
    assert "source_path" not in opened["properties"]
    for name in ("read_file", "list_dir", "shell", "ingest", "config"):
        assert name not in tools
    blocked = _err(_call(server, "study", {"action": "today"}))
    assert "study.enabled" in blocked
    assert not kb.settings.study_db.exists()
    listed = json.dumps(_rpc(server, "tools/list", {})["result"], ensure_ascii=False)
    assert str(kb.settings.storage_dir) not in listed
    assert kb.settings.llm_base_url not in listed

    kb.set_flag("study.enabled", True)
    assert "study" in _tools(server)
    today = _ok(_call(server, "study", {"action": "today"}))
    assert [row["id"] for row in today["items"]] == [card.id for card in HARDCODED_CARDS]
    today_text = json.dumps(today, ensure_ascii=False)
    assert "Retrieval-Augmented Generation" not in today_text
    assert all("answer" not in row for row in today["items"])
    secret_study = "/tmp/abs/secret-study.md"
    kb.study.today = lambda on=None: [
        {
            "id": "x",
            "stem": "题",
            "source_label": "examples/sample_notes/01.md",
            "source_path": secret_study,
            "image_path": "/tmp/abs/secret.png",
        }
    ]
    redacted = _ok(_call(server, "study", {"action": "today"}))
    assert redacted["items"][0]["source_path"] == ""
    assert redacted["items"][0]["image_path"] == ""
    assert redacted["items"][0]["source_label"] == "examples/sample_notes/01.md"
    assert secret_study not in json.dumps(redacted, ensure_ascii=False)
    answered = _ok(
        _call(
            server,
            "study",
            {
                "action": "answer",
                "item_id": "rag-fullname",
                "response": "Retrieval-Augmented Generation",
            },
        )
    )
    assert answered["correct"] == 1
    assert answered["item_id"] == "rag-fullname"
    rejected = _err(_call(server, "study", {"action": "enable"}))
    assert "today" in rejected
    assert kb.flag("study.enabled") is True

    kb.set_flag("study.enabled", False)
    assert "study" not in _tools(server)
    assert "study.enabled" in _err(_call(server, "study", {"action": "today"}))
    assert kb.settings.study_db.exists()


def test_mcp_search_and_ask_strip_absolute_paths_and_hide_documents(kb, tmp_path):
    kb.settings.expose_source_paths = False
    visible = tmp_path / "visible-cat.md"
    visible_text = "猫是常见的宠物动物，路径不应外泄。"
    visible.write_text(visible_text, encoding="utf-8")
    secret = "隐蔽的独角兽条款不会被检索到"
    hidden = tmp_path / "hidden-unicorn.md"
    hidden.write_text(secret, encoding="utf-8")
    visible_id = _ingest(kb, visible)
    hidden_id = _ingest(kb, hidden)
    assert kb.hide_document(hidden_id)
    private = tmp_path / "private-fox.md"
    private.write_text("私人狐狸不出现在默认检索里", encoding="utf-8")
    private_id = _ingest(kb, private, scope="private")

    launch = RecordingLaunch()
    server = McpServer(kb, launch=launch)
    hits = _ok(_call(server, "search", {"query": visible_text, "top_k": 5}))
    assert hits["hits"]
    assert all(hit["source_path"] == "" for hit in hits["hits"])
    assert {hit["doc_id"] for hit in hits["hits"]} == {visible_id}
    raw_hits = json.dumps(hits, ensure_ascii=False)
    assert str(visible) not in raw_hits
    assert str(hidden) not in raw_hits
    assert secret not in raw_hits
    assert private_id not in raw_hits

    answer = _ok(_call(server, "ask", {"query": visible_text, "top_k": 3}))
    assert answer["text"]
    assert answer["sources"]
    assert all(source["source_path"] == "" for source in answer["sources"])
    raw_answer = json.dumps(answer, ensure_ascii=False)
    assert str(visible) not in raw_answer
    assert str(kb.settings.storage_dir) not in raw_answer
    assert kb.settings.llm_api_key not in raw_answer
    assert secret not in raw_answer
    assert "full_text" not in answer

    hidden_hits = _ok(_call(server, "search", {"query": secret, "top_k": 5}))
    hidden_ids = {hit["doc_id"] for hit in hidden_hits["hits"]}
    assert hidden_id not in hidden_ids
    assert private_id not in hidden_ids
    assert secret not in json.dumps(hidden_hits, ensure_ascii=False)
    refused = _err(
        _call(server, "search", {"query": secret, "include_hidden": True})
    )
    assert "include_hidden" in refused
    still_hidden = _ok(_call(server, "search", {"query": secret, "top_k": 5}))
    still_ids = {hit["doc_id"] for hit in still_hidden["hits"]}
    assert hidden_id not in still_ids
    assert private_id not in still_ids
    assert secret not in json.dumps(still_hidden, ensure_ascii=False)

    open_error = _err(_call(server, "open_original", {"doc_id": hidden_id}))
    assert NOT_VISIBLE in open_error
    assert str(hidden) not in open_error
    assert secret not in open_error
    assert launch.calls == []
    path_rejected = _err(
        _call(server, "open_original", {"doc_id": visible_id, "path": str(visible)})
    )
    assert "path" in path_rejected
    assert launch.calls == []

    opened = _ok(_call(server, "open_original", {"doc_id": visible_id}))
    assert opened == {
        "opened": True,
        "doc_id": visible_id,
        "title": visible.stem,
        "opener": "obsidian",
    }
    assert "source_path" not in opened
    assert launch.calls == [("obsidian", visible.resolve())]
    assert str(visible) not in json.dumps(opened, ensure_ascii=False)
    assert _row(kb, visible_id)["full_text"] == ""
    assert _TAIL.encode() not in _sqlite_bytes(kb)


def test_mcp_keeps_relative_paths_and_can_expose_absolute_paths(kb, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    relative = Path("relative-note.md")
    text = "相对路径的笔记仍然可以检索。"
    relative.write_text(text, encoding="utf-8")
    doc_id = _ingest(kb, relative)
    kb.settings.expose_source_paths = False
    server = McpServer(kb, launch=RecordingLaunch(False))
    hits = _ok(_call(server, "search", {"query": text}))
    assert hits["hits"][0]["doc_id"] == doc_id
    assert hits["hits"][0]["source_path"] == "relative-note.md"
    assert str(tmp_path) not in json.dumps(hits, ensure_ascii=False)

    kb.settings.expose_source_paths = True
    absolute = tmp_path / "absolute-note.md"
    absolute_text = "打开调试开关后绝对路径才出现。"
    absolute.write_text(absolute_text, encoding="utf-8")
    absolute_id = _ingest(kb, absolute)
    exposed = _ok(_call(server, "search", {"query": absolute_text}))
    matched = [hit for hit in exposed["hits"] if hit["doc_id"] == absolute_id]
    assert matched and matched[0]["source_path"] == str(absolute)
    opened = _ok(_call(server, "open_original", {"doc_id": absolute_id}))
    assert "source_path" not in opened
    assert str(absolute) not in json.dumps(opened, ensure_ascii=False)


def test_mcp_stdio_handshake_and_unknown_method(kb):
    kb.settings.expose_source_paths = False
    init = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "0"},
        },
    }
    listed = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
    note = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    stdin = io.StringIO(
        json.dumps(init) + "\n\n" + json.dumps(note) + "\n" + json.dumps(listed) + "\n"
    )
    stdout = io.StringIO()
    serve_stdio(kb, stdin=stdin, stdout=stdout, launch=RecordingLaunch())
    lines = [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]
    assert len(lines) == 2
    assert lines[0]["result"]["protocolVersion"] == "2025-03-26"
    assert lines[0]["result"]["capabilities"]["tools"]["listChanged"] is True
    assert lines[0]["result"]["serverInfo"]["name"] == "kbagent"
    names = [tool["name"] for tool in lines[1]["result"]["tools"]]
    assert names == ["search", "ask", "open_original"]
    assert str(kb.settings.storage_dir) not in stdout.getvalue()

    server = McpServer(kb)
    fallback = server.handle(
        {
            "jsonrpc": "2.0",
            "id": "x",
            "method": "initialize",
            "params": {"protocolVersion": "1999-01-01"},
        }
    )
    assert fallback["result"]["protocolVersion"] == "2024-11-05"
    unknown = json.loads(
        dispatch_line(
            server,
            json.dumps({"jsonrpc": "2.0", "id": 9, "method": "resources/list"}),
        )
    )
    assert unknown["error"]["code"] == -32601
    parsed = json.loads(dispatch_line(server, "{"))
    assert parsed["error"]["code"] == -32700
    assert dispatch_line(server, json.dumps(note)) is None


def test_mcp_notifies_when_study_tools_change(kb):
    init = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": "2024-11-05", "capabilities": {}},
    }
    listed = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}

    class _Lines:
        def __init__(self) -> None:
            self.step = 0

        def __iter__(self):
            return self

        def __next__(self) -> str:
            self.step += 1
            if self.step == 1:
                return json.dumps(init) + "\n"
            if self.step == 2:
                kb.set_flag("study.enabled", True)
                return json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n"
            if self.step == 3:
                return json.dumps(listed) + "\n"
            raise StopIteration

    stdout = io.StringIO()
    serve_stdio(kb, stdin=_Lines(), stdout=stdout, launch=RecordingLaunch())
    lines = [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]
    assert [line.get("method") for line in lines] == [
        None,
        "notifications/tools/list_changed",
        None,
    ]
    assert lines[1] == {
        "jsonrpc": "2.0",
        "method": "notifications/tools/list_changed",
    }
    names = [tool["name"] for tool in lines[2]["result"]["tools"]]
    assert names == ["search", "ask", "open_original", "study"]
    assert str(kb.settings.storage_dir) not in stdout.getvalue()


def test_cli_open_and_mcp_commands(kb, tmp_path, monkeypatch):
    note = tmp_path / "cli-bound.md"
    note.write_text("命令行打开的绑定原文。", encoding="utf-8")
    doc_id = _ingest(kb, note)
    monkeypatch.setattr("kbagent.cli._kb", lambda settings=None: kb)
    created: list[RecordingLaunch] = []

    class _Factory(RecordingLaunch):
        def __init__(self):
            super().__init__(False)
            created.append(self)

    monkeypatch.setattr("kbagent.open_original.SystemOpenLaunch", _Factory)
    runner = CliRunner()
    opened = runner.invoke(app, ["open", doc_id])
    assert opened.exit_code == 0, opened.stdout
    assert "系统默认应用" in opened.stdout
    assert doc_id in opened.stdout
    assert str(note) not in (opened.stdout + (opened.stderr or ""))
    assert created[-1].calls == [("system", note.resolve())]
    assert _row(kb, doc_id)["full_text"] == ""

    missing = runner.invoke(app, ["open", "does-not-exist"])
    text = missing.stdout + (missing.stderr or "")
    assert missing.exit_code == 1
    assert NOT_VISIBLE in text

    help_open = runner.invoke(app, ["open", "--help"])
    assert help_open.exit_code == 0
    assert "文档 id" in help_open.stdout
    help_mcp = runner.invoke(app, ["mcp", "--help"])
    assert help_mcp.exit_code == 0
    assert "stdio" in help_mcp.stdout

    payload = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2024-11-05"},
        }
    )
    served = runner.invoke(app, ["mcp"], input=payload + "\n")
    assert served.exit_code == 0, served.stdout
    assert "2024-11-05" in served.stdout
    assert "kbagent" in served.stdout
    assert str(note) not in served.stdout
    assert str(kb.settings.storage_dir) not in served.stdout
