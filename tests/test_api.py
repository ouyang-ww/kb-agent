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
    )
    from kbagent.kb import KnowledgeBase

    kb = KnowledgeBase(settings, embedder=FakeEmbedder(), llm=FakeLLM())
    app = create_app(kb=kb, settings=settings)
    return TestClient(app)


def test_health(tmp_path):
    c = _make_client(tmp_path)
    assert c.get("/v1/health").json() == {"status": "ok"}


def test_ingest_text_and_get(tmp_path):
    c = _make_client(tmp_path)
    r = c.post("/v1/ingest", json={"content": "向量检索核心是 embedding。", "title": "t"})
    assert r.status_code == 200
    doc_id = r.json()["doc_id"]
    assert doc_id
    doc = c.get(f"/v1/documents/{doc_id}").json()
    assert doc["title"] == "t"
    assert "向量检索" in doc["full_text"]


def test_ingest_file(tmp_path):
    c = _make_client(tmp_path)
    files = {"file": ("note.txt", "文件内容测试".encode("utf-8"), "text/plain")}
    r = c.post("/v1/ingest/file", files=files)
    assert r.status_code == 200
    assert r.json()["doc_id"]


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

    c = _make_client(tmp_path, api_key="secret")
    assert c.get("/v1/stats", headers={"X-API-Key": "sec"}).status_code == 401
    assert c.get("/v1/stats", headers={"X-API-Key": "secret-extra"}).status_code == 401
    assert (
        c.post("/v1/ingest", json={"content": "x"}, headers={"X-API-Key": "s"}).status_code
        == 401
    )
    assert c.get("/v1/stats", headers={"X-API-Key": "secret"}).status_code == 200


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
