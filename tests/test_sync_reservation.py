"""同步通道预留：许可、稳定 id、本机路径映射。不连网，也不做移动端。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from kbagent.cli import app
from kbagent.errors import KBError, NotFoundError
from kbagent.ingestion.loaders import doc_id_for
from kbagent.sync.ids import mint_stable_id, parse_stable_id
from kbagent.sync.permissions import (
    CHANNEL_KINDS,
    KIND_HEALTH,
    KIND_ORIGINAL,
    allows,
    empty_grants,
)
from kbagent.sync.service import (
    COPIES_ORIGINAL,
    MOBILE_CLIENT,
    TRANSPORT_IMPLEMENTED,
    SyncReservation,
)

SENTINEL = "SYNC_BODY_SENTINEL_9f3a_不应进入同步库"


def _open(path: Path) -> SyncReservation:
    return SyncReservation(path)


def test_reservation_does_not_transport_or_copy_originals():
    assert TRANSPORT_IMPLEMENTED is False
    assert MOBILE_CLIENT is False
    assert COPIES_ORIGINAL is False
    package = Path(__file__).resolve().parents[1] / "src" / "kbagent" / "sync"
    banned = ("socket", "http.client", "urllib", "requests", "httpx", "websockets")
    for path in package.glob("*.py"):
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not (stripped.startswith("import ") or stripped.startswith("from ")):
                continue
            for name in banned:
                assert name not in stripped, path.name


def test_default_grants_deny_every_channel():
    grants = empty_grants()
    assert grants == {"remote": False, "lan": False, "health_remote": False}
    for kind in [*CHANNEL_KINDS, KIND_HEALTH, KIND_ORIGINAL, "shell", ""]:
        assert allows(grants, "remote", kind) is False
        assert allows(grants, "lan", kind) is False
    assert allows(grants, "cloud", "metadata") is False


def test_remote_and_lan_grants_stay_separate(tmp_path):
    reservation = _open(tmp_path / "sync.sqlite3")
    try:
        assert reservation.allows("remote", "metadata") is False
        reservation.grant("remote")
        for kind in CHANNEL_KINDS:
            assert reservation.allows("remote", kind) is True
            assert reservation.allows("lan", kind) is False
        assert reservation.allows("remote", KIND_HEALTH) is False
        assert reservation.allows("remote", KIND_ORIGINAL) is False
        assert reservation.allows("lan", KIND_HEALTH) is False

        reservation.grant("wifi")
        assert reservation.grants()["lan"] is True
        assert reservation.allows("lan", "study") is True
        assert reservation.allows("lan", KIND_HEALTH) is True
        assert reservation.allows("lan", KIND_ORIGINAL) is False
        assert reservation.allows("remote", KIND_HEALTH) is False
        assert reservation.allows("wi-fi", "timeline") is True
    finally:
        reservation.close()


def test_health_remote_needs_its_own_grant_and_an_open_channel(tmp_path):
    reservation = _open(tmp_path / "sync.sqlite3")
    try:
        reservation.grant("health-remote")
        assert reservation.grants()["health_remote"] is True
        assert reservation.allows("remote", KIND_HEALTH) is False
        assert reservation.allows("remote", "metadata") is False
        assert reservation.allows("lan", KIND_HEALTH) is False

        reservation.grant("remote")
        assert reservation.allows("remote", KIND_HEALTH) is True
        assert reservation.allows("remote", KIND_ORIGINAL) is False

        reservation.revoke("remote")
        assert reservation.grants()["health_remote"] is True
        assert reservation.allows("remote", KIND_HEALTH) is False
        assert reservation.allows("remote", "summary") is False

        reservation.grant("remote")
        assert reservation.allows("remote", KIND_HEALTH) is True
        reservation.revoke("health_remote")
        assert reservation.allows("remote", KIND_HEALTH) is False
        assert reservation.allows("remote", "chunks") is True
        assert reservation.allows("remote", "embeddings") is True
    finally:
        reservation.close()


def test_unknown_grant_is_rejected_without_opening_a_channel(tmp_path):
    reservation = _open(tmp_path / "sync.sqlite3")
    try:
        with pytest.raises(KBError):
            reservation.grant("health")
        with pytest.raises(KBError):
            reservation.grant("/tmp/vault")
        assert reservation.grants() == empty_grants()
    finally:
        reservation.close()


def test_grants_survive_reopen(tmp_path):
    path = tmp_path / "sync.sqlite3"
    first = _open(path)
    first.grant("lan")
    first.close()
    second = _open(path)
    try:
        assert second.grants()["lan"] is True
        assert second.grants()["remote"] is False
        second.revoke("lan")
    finally:
        second.close()
    third = _open(path)
    try:
        assert third.grants() == empty_grants()
    finally:
        third.close()


def test_stable_ids_are_not_paths_or_document_hashes(tmp_path):
    folder = tmp_path / "vault"
    folder.mkdir()
    note = folder / "note.md"
    note.write_text(SENTINEL, encoding="utf-8")
    local_doc = doc_id_for(str(note))

    for kind, prefix in (("device", "dev_"), ("vault", "vlt_"), ("record", "rec_")):
        stable_id = mint_stable_id(kind)
        assert stable_id.startswith(prefix)
        assert "/" not in stable_id
        assert str(tmp_path) not in stable_id
        assert parse_stable_id(stable_id, kind) == stable_id

    for bad in ("", str(note), "/tmp/vault", r"C:\Users\notes", "~/vault", local_doc, "rec_short"):
        with pytest.raises(KBError):
            parse_stable_id(bad)

    reservation = _open(tmp_path / "sync.sqlite3")
    try:
        device_id = reservation.device_id()
        assert device_id.startswith("dev_")
        assert str(folder) not in device_id
        vault_id = reservation.bind_path(folder)
        record_id = reservation.register_record(vault_id, "note.md", local_doc_id=local_doc)
        assert record_id.startswith("rec_")
        assert record_id != local_doc
        assert record_id != vault_id
        stored = reservation.record(record_id)
        assert stored["relative_path"] == "note.md"
        assert stored["local_doc_id"] == local_doc
        assert str(note) not in stored["relative_path"]
        again = reservation.register_record(vault_id, "note.md")
        assert again == record_id
        assert reservation.record(record_id)["local_doc_id"] == local_doc
    finally:
        reservation.close()

    blob = (tmp_path / "sync.sqlite3").read_bytes()
    assert SENTINEL.encode("utf-8") not in blob


def test_device_id_is_stable_and_local_to_the_database(tmp_path):
    first_path = tmp_path / "a" / "sync.sqlite3"
    second_path = tmp_path / "b" / "sync.sqlite3"
    first = _open(first_path)
    first_id = first.device_id()
    first.close()
    reopened = _open(first_path)
    try:
        assert reopened.device_id() == first_id
    finally:
        reopened.close()
    other = _open(second_path)
    try:
        assert other.device_id() != first_id
        assert "/" not in other.device_id()
    finally:
        other.close()


def test_local_path_is_a_mapping_not_the_primary_key(tmp_path):
    vault = tmp_path / "vault"
    nested = vault / "nested"
    nested.mkdir(parents=True)
    note = nested / "a.md"
    note.write_text(SENTINEL, encoding="utf-8")
    moved = tmp_path / "moved"
    moved.mkdir()

    reservation = _open(tmp_path / "sync.sqlite3")
    try:
        vault_id = reservation.bind_path(vault)
        assert reservation.bind_path(vault) == vault_id
        assert reservation.local_path(vault_id) == vault.resolve()
        record_id = reservation.register_record(vault_id, "nested/a.md")
        assert reservation.locate(note) == (vault_id, "nested/a.md")
        assert reservation.resolve(vault_id, "nested/a.md") == note.resolve()

        kept = reservation.rebind_path(vault_id, moved)
        assert kept == vault_id
        assert reservation.local_path(vault_id) == moved.resolve()
        assert reservation.record(record_id)["stable_id"] == record_id
        relocated = reservation.relocate_record(record_id, "b.md")
        assert relocated == record_id
        assert reservation.record(record_id)["relative_path"] == "b.md"
        assert reservation.resolve(vault_id, "b.md") == (moved / "b.md").resolve()

        outside = tmp_path / "other"
        outside.mkdir()
        other_id = reservation.bind_path(outside)
        assert other_id != vault_id
        with pytest.raises(KBError):
            reservation.rebind_path(vault_id, outside)
        assert reservation.locate(outside / "nope.md") == (other_id, "nope.md")
        assert reservation.locate(tmp_path / "missing-root" / "x.md") is None
    finally:
        reservation.close()


def test_nested_mapping_wins_and_escape_is_rejected(tmp_path):
    root = tmp_path / "vault"
    inner = root / "inner"
    inner.mkdir(parents=True)
    secret = tmp_path / "secret.txt"
    secret.write_text(SENTINEL, encoding="utf-8")
    link = inner / "escape.md"
    link.symlink_to(secret)

    reservation = _open(tmp_path / "sync.sqlite3")
    try:
        root_id = reservation.bind_path(root)
        inner_id = reservation.bind_path(inner)
        assert reservation.locate(inner / "keep.md") == (inner_id, "keep.md")
        assert reservation.locate(root / "top.md") == (root_id, "top.md")
        assert reservation.locate(link) is None
        with pytest.raises(KBError):
            reservation.register_record(inner_id, "escape.md")
        for relative in ("/etc/passwd", "../secret.txt", "a/../../secret.txt", "C:/Users/a.md", ""):
            with pytest.raises(KBError):
                reservation.register_record(root_id, relative)
        with pytest.raises(KBError):
            reservation.bind_path(secret)
        with pytest.raises(KBError):
            reservation.bind_path(tmp_path / "missing")
        with pytest.raises(KBError):
            reservation.register_record(str(root), "top.md")
    finally:
        reservation.close()

    blob = (tmp_path / "sync.sqlite3").read_bytes()
    assert SENTINEL.encode("utf-8") not in blob


def test_intermediate_symlink_with_missing_leaf_is_rejected(tmp_path, monkeypatch):
    """中间目录指向映射以外时，末端文件还不存在也要拒绝。"""
    root = tmp_path / "vault"
    outside = tmp_path / "OUTSIDE_SYNC_ROOT_9f3a"
    real = root / "real"
    root.mkdir()
    outside.mkdir()
    real.mkdir()
    (root / "link").symlink_to(outside, target_is_directory=True)
    (root / "alias").symlink_to(real, target_is_directory=True)
    (root / "broken").symlink_to(outside / "missing-dir", target_is_directory=True)

    reservation = _open(tmp_path / "sync.sqlite3")
    try:
        vault_id = reservation.bind_path(root)
        with pytest.raises(KBError) as escaped:
            reservation.register_record(vault_id, "link/missing.md")
        assert str(outside) not in str(escaped.value)
        assert str(root) not in str(escaped.value)
        with pytest.raises(KBError) as resolved:
            reservation.resolve(vault_id, "link/missing.md")
        assert str(outside) not in str(resolved.value)
        with pytest.raises(KBError) as broken:
            reservation.register_record(vault_id, "broken/missing.md")
        assert str(outside) not in str(broken.value)
        assert reservation.locate(root / "link" / "missing.md") is None
        kept = reservation.register_record(vault_id, "alias/new.md")
        assert kept.startswith("rec_")
        assert reservation.resolve(vault_id, "alias/new.md") == (real / "new.md").resolve()
        assert reservation.record(kept)["relative_path"] == "alias/new.md"
    finally:
        reservation.close()

    conn = sqlite3.connect(tmp_path / "sync.sqlite3")
    try:
        relatives = [
            row[0]
            for row in conn.execute("SELECT relative_path FROM records")
        ]
    finally:
        conn.close()
    assert relatives == ["alias/new.md"]
    blob = (tmp_path / "sync.sqlite3").read_bytes()
    assert b"OUTSIDE_SYNC_ROOT_9f3a" not in blob
    assert SENTINEL.encode("utf-8") not in blob

    storage = tmp_path / "kb"
    monkeypatch.setenv("KB_STORAGE_DIR", str(storage))
    runner = CliRunner()
    bound = runner.invoke(app, ["sync", "bind", str(root)])
    assert bound.exit_code == 0, bound.output
    vault_id = bound.stdout.strip().removeprefix("已映射 ").strip()
    failed = runner.invoke(app, ["sync", "id", vault_id, "link/missing.md"])
    assert failed.exit_code == 1
    assert "路径越出本机映射" in failed.output
    assert str(outside) not in failed.output
    assert str(root) not in failed.output
    assert "OUTSIDE_SYNC_ROOT_9f3a" not in failed.output
    assert b"OUTSIDE_SYNC_ROOT_9f3a" not in (storage / "sync.sqlite3").read_bytes()


def test_schema_has_no_original_text_and_path_is_not_the_key(tmp_path):
    path = tmp_path / "sync.sqlite3"
    reservation = _open(path)
    reservation.close()
    conn = sqlite3.connect(path)
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert {"device", "grants", "path_maps", "records"} <= tables
        for table in ("device", "grants", "path_maps", "records"):
            columns = [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
            assert "full_text" not in columns
            assert "content" not in columns
            assert "body" not in columns
        path_columns = list(conn.execute("PRAGMA table_info(path_maps)"))
        primary = [row[1] for row in path_columns if row[5] == 1]
        assert primary == ["stable_id"]
        record_columns = list(conn.execute("PRAGMA table_info(records)"))
        record_pk = [row[1] for row in record_columns if row[5] == 1]
        assert record_pk == ["stable_id"]
    finally:
        conn.close()


def test_core_ingest_does_not_open_sync_or_copy_bound_text(kb, tmp_path):
    assert not kb.settings.sync_db.exists()
    assert not kb.settings.study_db.exists()
    inline_id = kb.ingest_text("预留不参与摄入。", title="独立")
    assert inline_id
    assert kb.get_document(inline_id) is not None
    assert not kb.settings.sync_db.exists()

    body = "# 标题\n\n" + SENTINEL
    source = tmp_path / "vault" / "note.md"
    source.parent.mkdir()
    source.write_text(body, encoding="utf-8")
    doc_id = kb.ingest(source).added[0]
    doc = kb.get_document(doc_id)
    assert doc is not None
    assert doc.full_text == ""
    assert doc.source_path == str(source)

    vault_id = kb.sync.bind_path(source.parent)
    record_id = kb.sync.register_record(vault_id, "note.md", local_doc_id=doc_id)
    assert record_id.startswith("rec_")
    assert kb.get_document(doc_id).full_text == ""
    assert not kb.settings.study_db.exists()
    blob = kb.settings.sync_db.read_bytes()
    assert SENTINEL.encode("utf-8") not in blob
    kb.sync.grant("remote")
    conn = sqlite3.connect(kb.settings.documents_db)
    try:
        audits = conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE action LIKE 'sync%'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert audits == 0
    kb.close()


def test_cli_status_grant_bind_and_id_hide_absolute_paths(tmp_path, monkeypatch):
    storage = tmp_path / "kb"
    monkeypatch.setenv("KB_STORAGE_DIR", str(storage))
    vault = tmp_path / "vault"
    vault.mkdir()
    note = vault / "my notes" / "a.md"
    note.parent.mkdir()
    note.write_text(SENTINEL, encoding="utf-8")
    runner = CliRunner()

    status = runner.invoke(app, ["sync", "status"])
    assert status.exit_code == 0, status.output
    assert "传输 关" in status.stdout
    assert "移动端 无" in status.stdout
    assert "原件全文 不传" in status.stdout
    assert "remote=off" in status.stdout
    assert "lan=off" in status.stdout
    assert "health_remote=off" in status.stdout
    assert "设备 dev_" in status.stdout
    assert str(vault) not in status.stdout
    assert not (storage / "documents.sqlite3").exists()
    assert not (storage / "study.sqlite3").exists()

    unknown = runner.invoke(app, ["sync", "grant", "health"])
    assert unknown.exit_code == 1
    assert "未知的同步许可" in unknown.output

    granted = runner.invoke(app, ["sync", "grant", "remote"])
    assert granted.exit_code == 0, granted.output
    assert granted.stdout.strip() == "remote=on"
    health = runner.invoke(app, ["sync", "grant", "health-remote"])
    assert health.exit_code == 0, health.output
    assert health.stdout.strip() == "health_remote=on"
    revoked = runner.invoke(app, ["sync", "revoke", "remote"])
    assert revoked.exit_code == 0, revoked.output
    assert revoked.stdout.strip() == "remote=off"

    bound = runner.invoke(app, ["sync", "bind", str(vault)])
    assert bound.exit_code == 0, bound.output
    vault_id = bound.stdout.strip().removeprefix("已映射 ").strip()
    assert vault_id.startswith("vlt_")
    assert str(vault) not in bound.stdout
    assert SENTINEL not in bound.stdout

    listed = runner.invoke(app, ["sync", "paths"])
    assert listed.exit_code == 0, listed.output
    assert vault_id in listed.stdout
    assert str(vault) not in listed.stdout

    created = runner.invoke(app, ["sync", "id", vault_id, "my notes/a.md"])
    assert created.exit_code == 0, created.output
    assert created.stdout.strip().startswith("rec_")
    assert str(note) not in created.stdout
    assert SENTINEL not in created.output

    moved = tmp_path / "elsewhere"
    moved.mkdir()
    rebound = runner.invoke(app, ["sync", "rebind", vault_id, str(moved)])
    assert rebound.exit_code == 0, rebound.output
    assert vault_id in rebound.stdout
    assert str(moved) not in rebound.stdout

    escaped = runner.invoke(app, ["sync", "id", vault_id, "../secret.md"])
    assert escaped.exit_code == 1
    blob = (storage / "sync.sqlite3").read_bytes()
    assert SENTINEL.encode("utf-8") not in blob


def test_missing_record_and_unmapped_resolve(tmp_path):
    reservation = _open(tmp_path / "sync.sqlite3")
    try:
        with pytest.raises(NotFoundError):
            reservation.record("rec_" + "ab" * 16)
        folder = tmp_path / "vault"
        folder.mkdir()
        vault_id = reservation.bind_path(folder)
        with pytest.raises(KBError):
            reservation.resolve("vlt_" + "cd" * 16, "a.md")
        assert reservation.resolve(vault_id, "a.md") == (folder / "a.md").resolve()
    finally:
        reservation.close()
