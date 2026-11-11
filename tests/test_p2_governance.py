"""P2：闸 3、过期/冲突、近似去重、三套评测。不重做闸 1 / 闸 2。"""

from __future__ import annotations

from pathlib import Path

from kbagent.eval.dataset import load_dataset
import pytest

from kbagent.errors import IngestError
from kbagent.eval.metrics import acknowledges_missing_points
from kbagent.eval.runner import Evaluator
from kbagent.generation.answerability import INSUFFICIENT_EVIDENCE
from kbagent.governance.near_dup import RETUNE_NOTE, cosine, hamming, simhash64
from kbagent.governance.validity import OPEN_ENDED, is_expired
from kbagent.models import RetrievalHit
from kbagent.retrieval.diversity import select_diverse

_ROOT = Path(__file__).resolve().parents[1]


class _ScriptedEmbedder:
    def __init__(self, mapping: dict[str, list[float]]) -> None:
        self.mapping = mapping

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [list(self.mapping[text]) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return list(self.mapping[text])


class _Verdicts:
    def __init__(self, default: str = "not_entailed", allow: tuple[str, ...] = ()) -> None:
        self.default = default
        self.allow = allow
        self.calls = 0

    def verify_many(self, claims, snippets):
        self.calls += 1
        out = []
        for claim in claims:
            if any(piece in claim.text for piece in self.allow):
                out.append("entailed")
            else:
                out.append(self.default)
        return out


def test_thresholds_are_documented_defaults():
    assert "0.95" in RETUNE_NOTE
    assert "0.7" in RETUNE_NOTE
    assert "重标" in RETUNE_NOTE
    assert is_expired("2020-01-01") is True
    assert is_expired(OPEN_ENDED) is False
    assert is_expired(None) is False
    # 当天仍有效，次日才排除。
    assert is_expired("2026-11-11", "2026-11-11") is False
    assert is_expired("2026-11-11", "2026-11-12") is True


def test_strict_off_does_not_call_verifier(kb):
    kb.ingest_text("RAG 是检索增强生成。", title="RAG")
    verifier = _Verdicts()
    kb._pipeline.verifier = verifier
    kb.settings.answer_strict = False
    kb.settings.answer_tau_low = 0
    kb.settings.answer_tau_high = 0
    ans = kb.ask("什么是 RAG？")
    assert verifier.calls == 0
    assert "verification" not in ans.meta
    assert ans.meta["refusal_reason"] is None


def test_strict_without_key_skips(kb):
    kb.ingest_text("RAG 是检索增强生成。", title="RAG")
    kb.settings.answer_strict = True
    kb.settings.answer_tau_low = 0
    kb.settings.answer_tau_high = 0
    kb.settings.llm_api_key = ""
    kb._pipeline.verifier = None
    ans = kb.ask("什么是 RAG？")
    assert ans.meta["verification"]["skipped"] == "no_llm_key"
    assert ans.meta["refusal_reason"] is None
    assert "[1]" in ans.text
    row = kb.list_answers()[-1]
    assert row["verification"]["skipped"] == "no_llm_key"


def test_strict_drops_unentailed_claims_and_records_gaps(kb):
    kb.ingest_text("RAG 是检索增强生成。", title="RAG")
    kb.settings.answer_strict = True
    kb.settings.answer_tau_low = 0
    kb.settings.answer_tau_high = 0
    kb.settings.answer_strict_max_fail_rate = 0.9
    kb._llm.chat_text = "RAG 是检索增强生成 [1]。另外还需要自研芯片 [1]。"
    kb._pipeline.verifier = _Verdicts(default="not_entailed", allow=("检索增强",))
    ans = kb.ask("什么是 RAG？")
    assert "检索增强" in ans.text
    assert "自研芯片" not in ans.text
    assert ans.meta["gaps"]
    assert ans.meta["verification"]["downgraded"] is False
    assert ans.meta["refusal_reason"] is None
    assert any(row["verdict"] == "not_entailed" for row in ans.meta["verification"]["claims"])


def test_strict_high_failure_downgrades_to_refusal(kb):
    kb.ingest_text("RAG 是检索增强生成。", title="RAG")
    kb.settings.answer_strict = True
    kb.settings.answer_tau_low = 0
    kb.settings.answer_tau_high = 0
    kb.settings.answer_strict_max_fail_rate = 0.5
    kb._llm.chat_text = "完全编造的结论一 [1]。完全编造的结论二 [1]。"
    kb._pipeline.verifier = _Verdicts(default="not_entailed")
    ans = kb.ask("什么是 RAG？")
    assert ans.text == INSUFFICIENT_EVIDENCE
    assert ans.meta["refusal_reason"] == "unverified_claims"
    assert ans.meta["verification"]["downgraded"] is True
    assert ans.meta["answerability"] == "refuse"


def test_expired_doc_is_excluded_until_explicitly_requested(kb):
    old = kb.ingest_text(
        "差旅住宿旧标准是每晚 300 元。",
        title="住宿旧",
        metadata={"valid_until": "2020-01-01", "last_verified": "2019-05-01"},
    )
    new = kb.ingest_text("差旅住宿新标准是每晚 800 元。", title="住宿新")
    assert old and new
    hits = kb.search("差旅住宿每晚标准", top_k=5)
    assert old not in {hit.doc_id for hit in hits}
    assert new in {hit.doc_id for hit in hits}
    revealed = kb.search("差旅住宿每晚标准", top_k=5, include_expired=True)
    old_hits = [hit for hit in revealed if hit.doc_id == old]
    assert old_hits
    assert "2019-05-01" in old_hits[0].note
    assert "可能已过时" in old_hits[0].note
    # 文档行是第二道：切块仍是开放日期时，把 meta 改成过期也要排除。
    meta = kb.get_doc_meta(new)
    meta.valid_until = "2018-01-01"
    kb.governance.upsert_meta(meta)
    assert all(hit.doc_id != new for hit in kb.search("差旅住宿新标准是每晚 800 元", top_k=5))


def test_pending_supersedes_changes_nothing_and_confirmed_chain_follows_latest(kb):
    oldest = kb.ingest_text(
        "二零二三版食堂安排：周一午餐固定是红烧肉，餐补每月三百元。",
        title="食堂二零二三",
    )
    middle = kb.ingest_text(
        "二零二四版食堂安排：周一午餐改为清蒸鱼，餐补每月五百元。",
        title="食堂二零二四",
    )
    newest = kb.ingest_text(
        "二零二五版食堂安排：周一午餐改为蔬菜沙拉，餐补每月八百元。",
        title="食堂二零二五",
    )
    assert oldest and middle and newest
    kb.propose_relation(middle, oldest, "supersedes", created_by="user")
    kb.propose_relation(newest, middle, "supersedes", created_by="user")
    pending = kb.search("食堂安排周一午餐餐补", top_k=5)
    found = {hit.doc_id for hit in pending}
    assert {oldest, middle, newest} <= found
    assert kb.latest_doc(oldest) == oldest

    kb.confirm_relation(middle, oldest, "supersedes")
    kb.confirm_relation(newest, middle, "supersedes")
    assert kb.latest_doc(oldest) == newest
    assert kb.latest_doc(middle) == newest
    assert kb.get_doc_meta(oldest).status == "superseded"
    after = {hit.doc_id for hit in kb.search("食堂安排周一午餐餐补", top_k=5)}
    assert newest in after
    assert oldest not in after
    assert middle not in after

    kb.reject_relation(newest, middle, "supersedes")
    assert kb.get_doc_meta(middle).status == "active"
    restored = {hit.doc_id for hit in kb.search("食堂安排周一午餐餐补", top_k=5)}
    assert middle in restored
    assert oldest not in restored


def test_confirmed_contradiction_returns_both_accounts(kb):
    kb.settings.answer_tau_low = 0
    kb.settings.answer_tau_high = 0
    left = kb.ingest_text(
        "远程制度甲：每周最多远程一天，必须提前一天邮件审批。",
        title="远程甲",
    )
    right = kb.ingest_text(
        "远程制度乙：远程天数不设上限，无需审批。",
        title="远程乙",
    )
    kb.propose_relation(left, right, "contradicts", created_by="user")
    before = kb.ask("远程办公要不要审批？", top_k=5)
    assert before.meta.get("conflict") is not True
    kb.confirm_relation(left, right, "contradicts")
    ans = kb.ask("远程办公要不要审批？", top_k=5)
    assert ans.meta["conflict"] is True
    assert "存在两种说法" in ans.text
    assert {left, right} <= {source.doc_id for source in ans.sources}


def test_near_duplicate_document_is_not_indexed_again(kb):
    kept = kb.ingest_text("苹果是一种常见水果，味道清甜。", title="苹果")
    skipped = kb.ingest_text("苹果是一种常见水果，味道清甜。了", title="苹果副本")
    assert kept
    assert skipped is None
    rows = kb.list_near_duplicates(kind="document")
    assert rows
    assert rows[0]["duplicate_of"] == kept
    assert rows[0]["method"] in {"cosine", "simhash"}
    # 精确哈希仍然直接跳过，不记成近似重复。
    again = kb.ingest_text("苹果是一种常见水果，味道清甜。", title="苹果精确副本")
    assert again is None
    # 声明了冲突的近义文案不能被当成第二份索引吞掉。
    other = kb.ingest_text(
        "苹果是一种常见水果，味道清甜哦。",
        title="苹果另一种说法",
        metadata={"contradicts": kept},
    )
    assert other
    hits = kb.search("苹果是一种常见水果，味道清甜。", top_k=5)
    assert kept in {hit.doc_id for hit in hits}


def test_near_duplicate_chunks_do_not_dominate_top_k():
    embedder = _ScriptedEmbedder(
        {
            "same-a": [1.0, 0.0],
            "same-b": [0.9, 0.435889],
            "other": [0.0, 1.0],
        }
    )
    hits = [
        RetrievalHit(chunk_id="a", doc_id="a", text="same-a", score=0.99),
        RetrievalHit(chunk_id="b", doc_id="b", text="same-b", score=0.98),
        RetrievalHit(chunk_id="c", doc_id="c", text="other", score=0.70),
    ]
    # same-a 与 same-b 的余弦低于折叠阈值，靠 MMR 把多样的那条留下来。
    assert cosine([1.0, 0.0], [0.9, 0.435889]) < 0.95
    picked = select_diverse(
        hits, embedder, k=2, cosine_threshold=0.95, mmr_lambda=0.7
    )
    assert [hit.doc_id for hit in picked] == ["a", "c"]

    copies = [
        RetrievalHit(chunk_id="a", doc_id="a", text="苹果是一种常见水果，味道清甜。", score=0.99),
        RetrievalHit(chunk_id="b", doc_id="b", text="苹果是一种常见水果，味道清甜。了", score=0.98),
        RetrievalHit(chunk_id="d", doc_id="d", text="量子计算机需要稀释制冷机才能运行超导比特。", score=0.80),
    ]
    from helpers import FakeEmbedder

    folded = select_diverse(copies, FakeEmbedder(), k=2, cosine_threshold=0.95, mmr_lambda=0.7)
    assert "d" in {hit.doc_id for hit in folded}
    assert len({hit.doc_id for hit in folded} & {"a", "b"}) == 1
    assert hamming(simhash64(copies[0].text), simhash64(copies[1].text)) <= 3


def test_governance_eval_sets_run_without_dropping_refusal(kb):
    near = load_dataset(_ROOT / "eval/datasets/near_miss.jsonl")
    conflict = load_dataset(_ROOT / "eval/datasets/conflict.jsonl")
    expiry = load_dataset(_ROOT / "eval/datasets/expiry.jsonl")
    assert 10 <= len(near) <= 20
    assert 5 <= len(conflict) <= 10
    assert 5 <= len(expiry) <= 10
    assert all(item.documents for item in near)
    assert all(item.relations for item in conflict)

    evaluator = Evaluator(kb, kb.settings)
    conflict_result = evaluator.run(conflict, mode="conflict", rerank=False, top_k=5)
    assert conflict_result.metrics["conflict_presentation_rate"] == 1.0
    expiry_result = evaluator.run(expiry, mode="expiry", rerank=False, top_k=5)
    assert expiry_result.metrics["expiry_exclusion_rate"] == 1.0
    assert expiry_result.metrics["expiry_label_rate"] == 1.0
    assert kb.list_documents(include_hidden=True) == []

    skipped = kb.settings.model_copy(update={"llm_api_key": ""})
    near_result = Evaluator(kb, skipped).run(near[:2], mode="near_miss", rerank=False, top_k=3)
    assert near_result.metrics["partial_coverage_rate"] == -1.0
    assert near_result.warnings
    assert kb.list_documents(include_hidden=True) == []

    from kbagent.models import Answer

    def _weak(query, **kwargs):
        return Answer(text="（依据较弱：请核对原文后再采用。）", meta={})

    kb.ask = _weak
    missed = Evaluator(kb, kb.settings).run(near[:1], mode="near_miss", rerank=False, top_k=3)
    assert missed.metrics["partial_coverage_rate"] == 0.0
    assert acknowledges_missing_points("（依据较弱：请核对原文后再采用。）", {}, ["投影仪"]) is False

    def _named(query, **kwargs):
        return Answer(text="资料没有写投影仪。", meta={"gaps": ["投影仪"]})

    kb.ask = _named
    counted = Evaluator(kb, kb.settings).run(near[:1], mode="near_miss", rerank=False, top_k=3)
    assert counted.metrics["partial_coverage_rate"] == 1.0


def test_confirmed_relation_does_not_surface_private_or_expired(kb):
    kb.settings.answer_tau_low = 0
    kb.settings.answer_tau_high = 0
    public = kb.ingest_text("公开说法：远程每周最多一天，必须审批。", title="公开远程")
    private = kb.ingest_text(
        "秘密说法：远程天数不限，不必审批。", title="秘密远程", scope="private"
    )
    expired = kb.ingest_text(
        "过期说法：远程一律禁止，必须每天坐班。",
        title="过期远程",
        metadata={"valid_until": "2020-01-01"},
    )
    kb.propose_relation(public, private, "contradicts", created_by="user")
    kb.confirm_relation(public, private, "contradicts")
    kb.propose_relation(public, expired, "contradicts", created_by="user")
    kb.confirm_relation(public, expired, "contradicts")

    hits = kb.search("远程每周要不要审批", top_k=5)
    found = {hit.doc_id for hit in hits}
    assert public in found
    assert private not in found
    assert expired not in found
    ans = kb.ask("远程每周要不要审批", top_k=5)
    cited = {source.doc_id for source in ans.sources}
    assert private not in cited
    assert expired not in cited
    assert public in cited

    revealed = kb.search("远程每周要不要审批", top_k=5, include_hidden=True, include_expired=True)
    revealed_ids = {hit.doc_id for hit in revealed}
    assert private in revealed_ids
    assert expired in revealed_ids


def test_supersedes_to_hidden_or_expired_keeps_the_searchable_hit(kb):
    older = kb.ingest_text("旧的公开制度：食堂周一供应面条。", title="食堂旧")
    hidden_new = kb.ingest_text(
        "新的秘密制度：食堂周一供应海鲜。", title="食堂秘密", scope="private"
    )
    kb.governance.propose_relation(hidden_new, older, "supersedes", created_by="user")
    kb.governance.set_relation_status(hidden_new, older, "supersedes", "confirmed")
    assert kb.latest_doc(older) == hidden_new
    hits = kb.search("食堂周一供应", top_k=5)
    found = {hit.doc_id for hit in hits}
    assert older in found
    assert hidden_new not in found

    current = kb.ingest_text("现行制度：差旅每晚报销八百元。", title="差旅现行")
    stale = kb.ingest_text(
        "失效制度：差旅每晚只报销三百元。",
        title="差旅失效",
        metadata={"valid_until": "2019-01-01"},
    )
    kb.governance.propose_relation(stale, current, "supersedes", created_by="user")
    kb.governance.set_relation_status(stale, current, "supersedes", "confirmed")
    later = kb.search("差旅每晚报销", top_k=5)
    later_ids = {hit.doc_id for hit in later}
    assert current in later_ids
    assert stale not in later_ids


def test_unparseable_valid_until_is_rejected(kb):
    with pytest.raises(IngestError, match="valid_until"):
        kb.ingest_text("这条日期写错了。", title="坏日期", metadata={"valid_until": "2020/01/01"})
    assert kb.list_documents() == []


def test_near_dup_does_not_swallow_undeclared_conflict(kb):
    left = kb.ingest_text("公司年假一共 5 天，不含周末。", title="年假甲")
    right = kb.ingest_text("公司年假一共 15 天，不含周末。", title="年假乙")
    assert left and right and left != right
    hits = kb.search("公司年假一共几天", top_k=5)
    found = {hit.doc_id for hit in hits}
    assert left in found
    assert right in found


def test_equal_scores_keep_the_newer_verification():
    from helpers import FakeEmbedder

    text = "同一份说明，只是核对时间不同。"
    hits = [
        RetrievalHit(chunk_id="old", doc_id="old", text=text, score=0.9, last_verified=""),
        RetrievalHit(
            chunk_id="mid", doc_id="mid", text=text, score=0.9, last_verified="2020-01-01"
        ),
        RetrievalHit(
            chunk_id="new", doc_id="new", text=text, score=0.9, last_verified="2024-06-01"
        ),
    ]
    picked = select_diverse(hits, FakeEmbedder(), k=1, cosine_threshold=0.95, mmr_lambda=0.7)
    assert [hit.doc_id for hit in picked] == ["new"]


def test_partial_chunk_index_is_not_a_full_success(kb, tmp_path):
    kept = "苹果是一种常见水果，味道清甜呢。"
    kb.ingest_text(kept, title="已有苹果")
    kb._indexer.chunker.chunk_size = max(len(kept), 8)
    path = tmp_path / "mixed.md"
    path.write_text(kept + "\n\n" + ("量子比特需要稀释制冷机才能稳定运行。" * 6), encoding="utf-8")
    report = kb.ingest(path)
    assert report.partial
    assert report.ok is False
    assert report.partial[0]["doc_id"] in report.added
    assert report.partial[0]["indexed_chunks"] >= 1
    assert report.partial[0]["skipped_chunks"]

