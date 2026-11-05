"""REST API 测试：全端点 + 鉴权。"""

from fastapi.testclient import TestClient

from kbagent.api.server import create_app
from kbagent.config import Settings

from helpers import FakeEmbedder, FakeLLM


def _make_client(
    tmp_path,
    api_key: str = "",
    read_key: str = "",
    admin_key: str = "",
    **extra,
):
    settings = Settings(
        storage_dir=tmp_path / "kb",
        embedding_model="unused",
        rerank_enabled=False,
        intent_enabled=False,
        query_rewrite_enabled=False,
        multimodal_enabled=False,
        api_key=api_key,
        api_read_key=read_key,
        api_admin_key=admin_key,
        llm_api_key="test",
        llm_base_url="http://localhost:9999/v1",
        **extra,
    )
    from kbagent.kb import KnowledgeBase

    kb = KnowledgeBase(settings, embedder=FakeEmbedder(), llm=FakeLLM())
    app = create_app(kb=kb, settings=settings)
    return TestClient(app)


def test_health(tmp_path):
    c = _make_client(tmp_path)
    body = c.get("/v1/health").json()
    assert body["status"] == "ok"
    assert body["vision"] == {"enabled": False, "available": False, "error": None}


def test_ingest_text_and_get(tmp_path):
    c = _make_client(tmp_path)
    r = c.post("/v1/ingest", json={"content": "向量检索核心是 embedding。", "title": "t"})
    assert r.status_code == 200
    doc_id = r.json()["doc_id"]
    assert doc_id
    assert len(doc_id) == 64
    doc = c.get(f"/v1/documents/{doc_id}").json()
    assert doc["title"] == "t"
    assert doc["full_text"] is None
    revealed = c.get(f"/v1/documents/{doc_id}", params={"include_text": True}).json()
    assert "向量检索" in revealed["full_text"]


def test_ingest_file(tmp_path):
    c = _make_client(tmp_path)
    files = {"file": ("note.txt", "文件内容测试".encode("utf-8"), "text/plain")}
    r = c.post("/v1/ingest/file", files=files)
    assert r.status_code == 200
    assert r.json()["doc_id"]


def test_ingest_file_over_limit_is_413_and_leaves_no_temp(tmp_path):
    c = _make_client(tmp_path, max_upload_bytes=32)
    files = {"file": ("big.txt", b"x" * 64, "text/plain")}
    r = c.post("/v1/ingest/file", files=files)
    assert r.status_code == 413
    upload_dir = tmp_path / "kb" / "uploads"
    # 声明了大小时在落盘前拒绝，目录可以不存在。
    assert not upload_dir.exists() or list(upload_dir.iterdir()) == []


def test_ingest_file_stream_over_limit_deletes_partial(tmp_path, monkeypatch):
    """未声明大小时仍按流式计数拒绝，并删掉已经打开的临时文件。"""
    from starlette.datastructures import UploadFile

    real_init = UploadFile.__init__

    def hide_size(self, *args, **kwargs):
        real_init(self, *args, **kwargs)
        self.size = None

    monkeypatch.setattr(UploadFile, "__init__", hide_size)
    c = _make_client(tmp_path, max_upload_bytes=32)
    files = {"file": ("big.txt", b"x" * 64, "text/plain")}
    r = c.post("/v1/ingest/file", files=files)
    assert r.status_code == 413
    upload_dir = tmp_path / "kb" / "uploads"
    assert upload_dir.is_dir()
    assert list(upload_dir.iterdir()) == []


def test_ingest_file_at_limit_still_succeeds(tmp_path):
    c = _make_client(tmp_path, max_upload_bytes=64)
    files = {"file": ("ok.txt", b"y" * 64, "text/plain")}
    r = c.post("/v1/ingest/file", files=files)
    assert r.status_code == 200
    assert r.json()["doc_id"]
    upload_dir = tmp_path / "kb" / "uploads"
    assert list(upload_dir.iterdir()) == []


def test_ingest_empty_422(tmp_path):
    c = _make_client(tmp_path)
    assert c.post("/v1/ingest", json={"content": "  "}).status_code == 422


def test_search_and_ask(tmp_path):
    c = _make_client(tmp_path)
    c.post("/v1/ingest", json={"content": "猫是常见的宠物动物。", "title": "猫"})
    r = c.post("/v1/search", json={"query": "猫是常见的宠物动物。", "top_k": 2})
    assert r.status_code == 200
    assert len(r.json()["hits"]) == 1
    r = c.post("/v1/ask", json={"query": "猫是什么？"})
    assert r.status_code == 200
    body = r.json()
    assert body["text"]
    assert body["sources"]
    assert body["meta"]["intent"] == "kb_question"


def test_documents_list_delete_stats(tmp_path):
    c = _make_client(tmp_path)
    doc_id = c.post("/v1/ingest", json={"content": "要删除的内容"}).json()["doc_id"]
    assert len(c.get("/v1/documents").json()) == 1
    assert c.delete(f"/v1/documents/{doc_id}").status_code == 200
    assert c.get(f"/v1/documents/{doc_id}").status_code == 404
    assert c.delete("/v1/documents/not-exist").status_code == 404
    s = c.get("/v1/stats").json()
    assert s["documents"] == 0


def test_refresh_document(tmp_path):
    c = _make_client(tmp_path)
    doc_id = c.post("/v1/ingest", json={"content": "旧内容"}).json()["doc_id"]
    r = c.post(
        f"/v1/documents/{doc_id}/refresh", json={"content": "新内容更详细"}
    )
    assert r.status_code == 200
    new_id = r.json()["doc_id"]
    assert new_id != doc_id


def test_auth_required(tmp_path):
    c = _make_client(tmp_path, api_key="secret")
    assert c.get("/v1/stats").status_code == 401
    assert c.get("/v1/stats", headers={"X-API-Key": "wrong"}).status_code == 401
    assert c.get("/v1/stats", headers={"X-API-Key": "secret"}).status_code == 200


def test_unequal_length_api_key_is_rejected(tmp_path):
    """更短或更长的 Key 必须是 401，不能因为比较函数抛错变成 500。"""
    from kbagent.api.server import _key_matches

    assert _key_matches("sec", "secret") is False
    assert _key_matches("secret-extra", "secret") is False
    assert _key_matches("密" * len("secret"), "secret") is False
    assert _key_matches("secret", "secret") is True
    # HTTP 头本身只能是 ASCII；这里直接覆盖函数，避免 str compare_digest 的 TypeError。
    assert _key_matches("密钥-secret", "密钥-secret") is True
    assert _key_matches("密钥", "密钥-secret") is False

    c = _make_client(tmp_path, api_key="secret")
    assert c.get("/v1/stats", headers={"X-API-Key": "sec"}).status_code == 401
    assert c.get("/v1/stats", headers={"X-API-Key": "secret-extra"}).status_code == 401
    assert (
        c.post("/v1/ingest", json={"content": "x"}, headers={"X-API-Key": "s"}).status_code
        == 401
    )
    assert c.get("/v1/stats", headers={"X-API-Key": "secret"}).status_code == 200


def test_key_compare_exception_stays_unauthorized(tmp_path, monkeypatch):
    """3.10–3.12 的 str compare_digest 会 ValueError；比较失败必须仍是 401。"""
    import kbagent.api.server as server

    def boom(left, right):
        raise ValueError("lengths must match")

    monkeypatch.setattr(server.secrets, "compare_digest", boom)
    assert server._key_matches("ab", "abcd") is False
    assert server._key_matches("secret", "secret") is False

    c = _make_client(tmp_path, api_key="secret")
    assert c.get("/v1/stats", headers={"X-API-Key": "nope"}).status_code == 401
    assert c.get("/v1/stats", headers={"X-API-Key": "secret"}).status_code == 401


def test_runtime_flags_api(tmp_path):
    c = _make_client(tmp_path)
    r = c.get("/v1/settings/flags")
    assert r.status_code == 200
    assert r.json()["flags"]["study.enabled"] is False

    r = c.put("/v1/settings/flags/study.enabled", json={"enabled": True})
    assert r.status_code == 200
    assert r.json() == {"name": "study.enabled", "enabled": True}
    assert c.get("/v1/settings/flags").json()["flags"]["study.enabled"] is True

    assert (
        c.put("/v1/settings/flags/study.unknown", json={"enabled": True}).status_code
        == 400
    )


def test_read_and_admin_keys(tmp_path):
    c = _make_client(tmp_path, read_key="read", admin_key="admin")
    read = {"X-API-Key": "read"}
    admin = {"X-API-Key": "admin"}

    assert c.get("/v1/settings/flags", headers=read).status_code == 200
    assert c.get("/v1/settings/embedding", headers=read).status_code == 200
    assert c.post("/v1/search", json={"query": "x"}, headers=read).status_code == 200
    assert (
        c.post("/v1/ingest", json={"content": "x"}, headers=read).status_code == 401
    )
    assert (
        c.post("/v1/ingest", json={"content": "x"}, headers=admin).status_code == 200
    )
    assert (
        c.put(
            "/v1/settings/flags/study.enabled",
            json={"enabled": True},
            headers=read,
        ).status_code
        == 401
    )
    assert (
        c.put(
            "/v1/settings/flags/study.enabled",
            json={"enabled": True},
            headers=admin,
        ).status_code
        == 200
    )
    assert c.get("/v1/answer-log", headers=read).status_code == 401
    assert c.get("/v1/audit-log", headers=admin).status_code == 200
    assert c.get("/v1/audit-log", headers=read).status_code == 401


def test_answer_and_audit_logs_api(tmp_path):
    c = _make_client(tmp_path)
    doc_id = c.post("/v1/ingest", json={"content": "苹果是水果。"}).json()["doc_id"]
    c.post("/v1/ask", json={"query": "苹果是什么？"})
    answers = c.get("/v1/answer-log").json()["answers"]
    assert answers
    assert answers[0]["answer"]
    assert answers[0]["citations"]

    assert c.post(f"/v1/documents/{doc_id}/hide").status_code == 200
    entries = c.get("/v1/audit-log").json()["entries"]
    assert any(e["action"] == "document.hide" for e in entries)

    r = c.post("/v1/governance/backfill")
    assert r.status_code == 200
    assert r.json()["created"] == 0


def test_private_and_hidden_documents_api(tmp_path):
    c = _make_client(tmp_path)
    private = c.post(
        "/v1/ingest",
        json={"content": "秘密苹果内容", "title": "秘密", "scope": "private"},
    ).json()["doc_id"]
    public = c.post(
        "/v1/ingest", json={"content": "公开苹果内容", "title": "公开"}
    ).json()["doc_id"]

    assert [d["doc_id"] for d in c.get("/v1/documents").json()] == [public]
    assert c.get(f"/v1/documents/{private}").status_code == 404
    assert (
        c.post(
            f"/v1/documents/{private}/refresh", json={"content": "试图刷新"}
        ).status_code
        == 404
    )
    hits = c.post("/v1/search", json={"query": "苹果", "top_k": 5}).json()["hits"]
    assert hits
    assert all(h["doc_id"] != private for h in hits)

    assert c.post(f"/v1/documents/{public}/hide").status_code == 200
    assert c.get(f"/v1/documents/{public}").status_code == 404
    assert c.post("/v1/search", json={"query": "苹果", "top_k": 5}).json()["hits"] == []
    assert c.post(f"/v1/documents/{public}/unhide").status_code == 200
    assert c.get(f"/v1/documents/{public}").status_code == 200


def test_public_source_path_keeps_relative_paths():
    from kbagent.api.server import public_source_path

    assert public_source_path("examples/note.md", False) == "examples/note.md"
    assert public_source_path("/tmp/note.md", False) == ""
    assert public_source_path("/tmp/note.md", True) == "/tmp/note.md"
    assert public_source_path("", False) == ""


def test_responses_strip_absolute_source_path(tmp_path):
    from kbagent.kb import KnowledgeBase

    note = tmp_path / "note.md"
    note.write_text("猫是常见的宠物动物，路径不应外泄。", encoding="utf-8")
    settings = Settings(
        storage_dir=tmp_path / "kb",
        embedding_model="unused",
        rerank_enabled=False,
        intent_enabled=False,
        query_rewrite_enabled=False,
        multimodal_enabled=False,
        llm_api_key="test",
        llm_base_url="http://localhost:9999/v1",
    )
    kb = KnowledgeBase(settings, embedder=FakeEmbedder(), llm=FakeLLM())
    try:
        doc_id = kb.ingest(note).added[0]
        hidden = TestClient(create_app(kb=kb, settings=settings))
        listed = hidden.get("/v1/documents").json()
        assert listed[0]["source_path"] == ""
        body = hidden.get(f"/v1/documents/{doc_id}").json()
        assert body["source_path"] == ""
        assert str(note) not in str(body)
        hits = hidden.post(
            "/v1/search", json={"query": "猫是常见的宠物动物", "top_k": 2}
        ).json()["hits"]
        assert hits
        assert all(hit["source_path"] == "" for hit in hits)
        assert all(str(note) not in str(hit) for hit in hits)
        sources = hidden.post("/v1/ask", json={"query": "猫是什么？"}).json()["sources"]
        assert sources
        assert all(item["source_path"] == "" for item in sources)

        exposed_settings = settings.model_copy(update={"expose_source_paths": True})
        exposed = TestClient(create_app(kb=kb, settings=exposed_settings))
        shown = exposed.get(f"/v1/documents/{doc_id}").json()
        assert shown["source_path"] == str(note)
    finally:
        kb.close()


def test_loopback_names():
    from kbagent.api.server import is_loopback_host

    assert is_loopback_host("127.0.0.1")
    assert is_loopback_host("localhost")
    assert is_loopback_host("::1")
    assert is_loopback_host("[::1]")
    assert not is_loopback_host("0.0.0.0")
    assert not is_loopback_host("::")
    assert not is_loopback_host("192.168.1.10")


def test_non_loopback_without_keys_refuses(tmp_path):
    import pytest

    from kbagent.errors import ConfigError
    from kbagent.kb import KnowledgeBase

    kb = KnowledgeBase(
        Settings(
            storage_dir=tmp_path / "kb",
            embedding_model="unused",
            rerank_enabled=False,
            intent_enabled=False,
            query_rewrite_enabled=False,
            multimodal_enabled=False,
            llm_api_key="test",
            llm_base_url="http://localhost:9999/v1",
        ),
        embedder=FakeEmbedder(),
        llm=FakeLLM(),
    )
    try:
        with pytest.raises(ConfigError, match="拒绝"):
            create_app(kb=kb, settings=kb.settings, bind_host="0.0.0.0")
        public = kb.settings.model_copy(update={"api_host": "0.0.0.0"})
        with pytest.raises(ConfigError, match="拒绝"):
            create_app(kb=kb, settings=public)
    finally:
        kb.close()


def test_loopback_without_keys_warns_and_serves(tmp_path, caplog):
    import logging

    from kbagent.kb import KnowledgeBase

    kb = KnowledgeBase(
        Settings(
            storage_dir=tmp_path / "kb",
            embedding_model="unused",
            rerank_enabled=False,
            intent_enabled=False,
            query_rewrite_enabled=False,
            multimodal_enabled=False,
            llm_api_key="test",
            llm_base_url="http://localhost:9999/v1",
        ),
        embedder=FakeEmbedder(),
        llm=FakeLLM(),
    )
    try:
        with caplog.at_level(logging.WARNING, logger="kbagent.api.server"):
            app = create_app(kb=kb, settings=kb.settings, bind_host="127.0.0.1")
        assert any("鉴权已关闭" in record.getMessage() for record in caplog.records)
        client = TestClient(app)
        assert client.get("/v1/health").status_code == 200
        assert client.post("/v1/ingest", json={"content": "本机开发"}).status_code == 200
    finally:
        kb.close()


def test_require_auth_refuses_loopback_without_keys(tmp_path):
    import pytest

    from kbagent.errors import ConfigError
    from kbagent.kb import KnowledgeBase

    settings = Settings(
        storage_dir=tmp_path / "kb",
        embedding_model="unused",
        rerank_enabled=False,
        intent_enabled=False,
        query_rewrite_enabled=False,
        multimodal_enabled=False,
        api_require_auth=True,
        llm_api_key="test",
        llm_base_url="http://localhost:9999/v1",
    )
    kb = KnowledgeBase(settings, embedder=FakeEmbedder(), llm=FakeLLM())
    try:
        with pytest.raises(ConfigError, match="KB_API_REQUIRE_AUTH"):
            create_app(kb=kb, settings=settings, bind_host="localhost")
    finally:
        kb.close()


def test_non_loopback_with_key_requires_header(tmp_path):
    from kbagent.kb import KnowledgeBase

    settings = Settings(
        storage_dir=tmp_path / "kb",
        embedding_model="unused",
        rerank_enabled=False,
        intent_enabled=False,
        query_rewrite_enabled=False,
        multimodal_enabled=False,
        api_key="secret",
        api_host="0.0.0.0",
        llm_api_key="test",
        llm_base_url="http://localhost:9999/v1",
    )
    kb = KnowledgeBase(settings, embedder=FakeEmbedder(), llm=FakeLLM())
    try:
        client = TestClient(create_app(kb=kb, settings=settings, bind_host="0.0.0.0"))
        assert client.get("/v1/stats").status_code == 401
        assert client.post("/v1/ingest", json={"content": "x"}).status_code == 401
        assert (
            client.get("/v1/stats", headers={"X-API-Key": "secret"}).status_code == 200
        )
    finally:
        kb.close()


def test_cli_serve_refuses_public_bind_without_keys(monkeypatch):
    from typer.testing import CliRunner

    from kbagent.cli import app as cli_app

    for name in ("KB_API_KEY", "KB_API_READ_KEY", "KB_API_ADMIN_KEY", "KB_API_REQUIRE_AUTH"):
        monkeypatch.delenv(name, raising=False)

    def fail_create(*args, **kwargs):
        raise AssertionError("无 Key 时不应启动对外服务")

    monkeypatch.setattr("kbagent.api.server.create_app", fail_create)
    result = CliRunner().invoke(cli_app, ["serve", "--host", "0.0.0.0", "--port", "9"])
    assert result.exit_code != 0
    assert "拒绝" in result.output


def test_cli_serve_loopback_prints_warning(monkeypatch):
    import uvicorn
    from typer.testing import CliRunner

    from kbagent.cli import app as cli_app

    for name in ("KB_API_KEY", "KB_API_READ_KEY", "KB_API_ADMIN_KEY", "KB_API_REQUIRE_AUTH"):
        monkeypatch.delenv(name, raising=False)
    started: dict = {}

    def fake_create_app(*args, **kwargs):
        started["bind"] = kwargs.get("bind_host")
        return "app"

    monkeypatch.setattr("kbagent.api.server.create_app", fake_create_app)
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: started.setdefault("ran", True))
    result = CliRunner().invoke(cli_app, ["serve", "--host", "127.0.0.1", "--port", "8711"])
    assert result.exit_code == 0, result.output
    assert "鉴权已关闭" in result.output
    assert started["bind"] == "127.0.0.1"
    assert started["ran"] is True


def test_vision_init_failure_visible_in_health(tmp_path, caplog, monkeypatch):
    import logging

    from kbagent.kb import KnowledgeBase

    class Boom:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("caption backend missing")

    monkeypatch.setattr("kbagent.kb.VisionIndexer", Boom)
    settings = Settings(
        storage_dir=tmp_path / "kb",
        embedding_model="unused",
        rerank_enabled=False,
        intent_enabled=False,
        query_rewrite_enabled=False,
        multimodal_enabled=True,
        llm_api_key="test",
        llm_base_url="http://localhost:9999/v1",
    )
    with caplog.at_level(logging.WARNING, logger="kbagent.kb"):
        kb = KnowledgeBase(settings, embedder=FakeEmbedder(), llm=FakeLLM())
    try:
        warnings = [
            record
            for record in caplog.records
            if record.levelno >= logging.WARNING and "VisionIndexer" in record.getMessage()
        ]
        assert warnings
        assert warnings[0].exc_info is not None
        assert "caption backend missing" in (kb.vision_status()["error"] or "")
        client = TestClient(create_app(kb=kb, settings=settings))
        health = client.get("/v1/health").json()
        assert health["status"] == "degraded"
        assert health["vision"]["enabled"] is True
        assert health["vision"]["available"] is False
        assert "caption backend missing" in health["vision"]["error"]
        stats = client.get("/v1/stats").json()
        assert stats["vision"]["available"] is False
        assert "caption backend missing" in stats["vision"]["error"]
    finally:
        kb.close()
