"""命令行入口：kb ingest / search / ask / list / delete / stats / serve / chat / eval / eval-build。"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import typer
from typing import Optional

from .config import Settings
from .errors import KBError
from .eval.dataset import EvalItem, load_dataset, save_dataset, validate
from .eval.reports import compare_to_markdown, save_json, save_markdown, single_to_markdown
from .eval.runner import Evaluator
from .kb import KnowledgeBase

app = typer.Typer(add_completion=False, help="个人知识库 RAG Agent")
config_app = typer.Typer(help="运行时配置（热切换，无需重启）")
study_app = typer.Typer(help="学习扩展（可选，默认关闭）")
governance_app = typer.Typer(help="治理工具")
app.add_typer(config_app, name="config")
app.add_typer(study_app, name="study")
app.add_typer(governance_app, name="governance")


def _kb(settings: Settings | None = None) -> KnowledgeBase:
    return KnowledgeBase(settings)


def _parse_compare(spec: str) -> list[dict]:
    """解析 --compare "rerank=on,off;top_k=3,5" 为参数矩阵。"""
    params: dict[str, list] = {}
    for group in spec.split(";"):
        if not group.strip():
            continue
        key, _, values = group.strip().partition("=")
        key = key.strip()
        if not key or not values:
            raise typer.BadParameter(f"无法解析对比参数: {group}")
        vals: list = []
        for v in values.split(","):
            v = v.strip()
            if v in ("on", "true", "True"):
                vals.append(True)
            elif v in ("off", "false", "False"):
                vals.append(False)
            else:
                try:
                    vals.append(int(v))
                except ValueError:
                    vals.append(v)
        params[key] = vals
    import itertools

    keys = [*params.keys()]  # 注意：不能用内置 list()（被本模块 list 命令遮蔽）
    combos: list[dict] = []
    for values in itertools.product(*[params[k] for k in keys]):
        combos.append(dict(zip(keys, values)))
    return combos


# ================= ingest =================
@app.command()
def ingest(
    path: str = typer.Argument(..., help="文件或目录路径"),
    collection: Optional[str] = typer.Option(None, help="集合名（默认 KB_DEFAULT_COLLECTION）"),
    force: bool = typer.Option(False, help="内容相同也强制重建"),
    no_recursive: bool = typer.Option(False, help="不递归子目录"),
    scope: Optional[str] = typer.Option(None, help="可见范围：default / private / secret"),
):
    """导入文件/目录到知识库（md/txt/pdf，自动去重）。"""
    kb = _kb()
    t0 = time.perf_counter()
    report = kb.ingest(
        path,
        collection=collection,
        force=force,
        recursive=not no_recursive,
        scope=scope,
    )
    cost = time.perf_counter() - t0
    typer.echo(f"新增 {len(report.added)} 篇，跳过 {len(report.skipped)} 篇，失败 {len(report.failed)} 篇（耗时 {cost:.1f}s）")
    for did in report.added:
        typer.echo(f"  + {did}")
    for f in report.failed:
        typer.echo(f"  ! {f['path']}: {f['error']}", err=True)


# ================= search =================
@app.command()
def search(
    query: str = typer.Argument(...),
    top_k: int = typer.Option(None, help="返回条数（默认 KB_TOP_K）"),
    collection: Optional[str] = typer.Option(None),
    no_rerank: bool = typer.Option(False, help="关闭重排"),
    no_rewrite: bool = typer.Option(False, help="关闭查询改写"),
    include_hidden: bool = typer.Option(False, "--include-hidden", help="包含隐藏/私有文档"),
):
    """语义检索（纯检索，不生成答案）。"""
    kb = _kb()
    hits = kb.search(
        query, top_k=top_k, collection=collection,
        rerank=not no_rerank, rewrite=not no_rewrite,
        include_hidden=include_hidden,
    )
    if not hits:
        typer.echo("未找到相关内容")
        raise typer.Exit(0)
    for i, h in enumerate(hits, 1):
        media = f"  [图片: {h.media.image_path}]" if h.media else ""
        typer.echo(f"[{i}] 得分 {h.score:.4f} | {h.title} | {h.doc_id}{media}")
        if h.section:
            typer.echo(f"    章节: {h.section}")
        typer.echo(f"    {h.text[:150].replace(chr(10), ' ')}...")


# ================= ask =================
@app.command()
def ask(
    query: str = typer.Argument(...),
    top_k: int = typer.Option(None),
    collection: Optional[str] = typer.Option(None),
    no_rerank: bool = typer.Option(False),
    no_rewrite: bool = typer.Option(False),
    include_hidden: bool = typer.Option(False, "--include-hidden", help="包含隐藏/私有文档"),
):
    """RAG 问答（带出处引用）。"""
    kb = _kb()
    ans = kb.ask(
        query, top_k=top_k, collection=collection,
        rerank=not no_rerank, rewrite=not no_rewrite,
        include_hidden=include_hidden,
    )
    typer.echo(ans.text)
    typer.echo("")
    if ans.meta:
        typer.echo(f"# 意图: {ans.meta.get('intent')} | 改写后查询: {ans.meta.get('rewritten_query')}")
    for i, s in enumerate(ans.sources, 1):
        typer.echo(f"[{i}] {s.title} (score={s.score:.3f}) doc_id={s.doc_id}")
        if s.media:
            typer.echo(f"    图片: {s.media.image_path}")
        typer.echo(f"    {s.snippet[:120].replace(chr(10), ' ')}...")


# ================= 管理 =================
@app.command()
def list(
    collection: Optional[str] = typer.Option(None),
    include_hidden: bool = typer.Option(False, "--include-hidden", help="包含隐藏/私有文档"),
):
    """列出知识库文档。"""
    kb = _kb()
    docs = kb.list_documents(collection, include_hidden=include_hidden)
    if not docs:
        typer.echo("知识库为空")
        raise typer.Exit(0)
    width = max(14, *(len(d.doc_id) for d in docs))
    typer.echo(f"{'doc_id':<{width}} {'chunks':>6}  {'media':>5}  title")
    for d in docs:
        typer.echo(
            f"{d.doc_id:<{width}} {d.chunk_count:>6}  {d.media_count:>5}  {d.title}"
        )


@app.command()
def delete(
    doc_id: str = typer.Argument(...),
    collection: Optional[str] = typer.Option(None),
    include_hidden: bool = typer.Option(False, "--include-hidden", help="允许删除隐藏/私有文档"),
):
    """删除文档（级联删除子 chunk 与图片）。"""
    kb = _kb()
    ok = kb.delete_document(doc_id, collection, include_hidden=include_hidden)
    typer.echo(f"已删除 {doc_id}" if ok else f"文档不存在: {doc_id}", err=not ok)
    if not ok:
        raise typer.Exit(1)


@app.command()
def hide(doc_id: str = typer.Argument(...)):
    """隐藏文档（不再出现在检索/列表/问答中）。"""
    kb = _kb()
    ok = kb.hide_document(doc_id)
    typer.echo(f"已隐藏 {doc_id}" if ok else f"文档不存在: {doc_id}", err=not ok)
    if not ok:
        raise typer.Exit(1)


@app.command()
def unhide(doc_id: str = typer.Argument(...)):
    """恢复隐藏文档。"""
    kb = _kb()
    ok = kb.unhide_document(doc_id)
    typer.echo(f"已恢复 {doc_id}" if ok else f"文档不存在: {doc_id}", err=not ok)
    if not ok:
        raise typer.Exit(1)


@app.command()
def events(
    after: int = typer.Option(0, help="只显示 id > after 的事件"),
    limit: int = typer.Option(50),
):
    """查看治理事件（ingest / hide / unhide / delete）。"""
    kb = _kb()
    rows = kb.list_events(after_id=after, limit=limit)
    typer.echo(json.dumps(rows, ensure_ascii=False, indent=2))


@app.command()
def embedding():
    """查看当前激活的 embedding manifest。"""
    kb = _kb()
    typer.echo(json.dumps(kb.get_embedding_manifest(), ensure_ascii=False, indent=2))


@app.command("answer-log")
def answer_log(
    after: int = typer.Option(0, help="只显示 id > after 的记录"),
    limit: int = typer.Option(50),
):
    """查看答案日志。"""
    kb = _kb()
    typer.echo(json.dumps(kb.list_answers(after, limit), ensure_ascii=False, indent=2))


@app.command("audit-log")
def audit_log(
    after: int = typer.Option(0, help="只显示 id > after 的记录"),
    limit: int = typer.Option(50),
):
    """查看审计日志。"""
    kb = _kb()
    typer.echo(json.dumps(kb.list_audit(after, limit), ensure_ascii=False, indent=2))


@governance_app.command("backfill")
def governance_backfill():
    """为旧文档补充 docs_meta（缺失时按文档字段生成）。"""
    kb = _kb()
    created = kb.backfill_governance()
    typer.echo(f"已补充 {created} 条 docs_meta")


@governance_app.command("migrate-parents")
def governance_migrate_parents():
    """迁出已入库父全文。哈希一致才改为回读；不一致标为 diverged。"""
    kb = _kb()
    report = kb.migrate_parent_bodies()
    typer.echo(json.dumps(report, ensure_ascii=False))


@app.command()
def stats():
    """知识库统计。"""
    kb = _kb()
    s = kb.stats()
    typer.echo(json.dumps(s, ensure_ascii=False, indent=2))


@app.command()
def repair():
    """对账文档库与向量库。

    恢复未写完的替换（旧文档仍可检索）、清理没有父文档的向量，
    并删除已标记待清理的旧文档。可重复执行。
    """
    kb = _kb()
    report = kb.repair()
    typer.echo(json.dumps(report, ensure_ascii=False, indent=2))
    if report.get("errors") or report.get("vectorless"):
        raise typer.Exit(1)


# ================= 运行时开关 =================
@config_app.command("get")
def config_get(name: Optional[str] = typer.Argument(None)):
    """查看运行时开关；不传名称则列出全部。"""
    kb = _kb()
    try:
        if name:
            typer.echo("on" if kb.flag(name) else "off")
            return
    except KBError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    for key, enabled in kb.get_flags().items():
        typer.echo(f"{key}={'on' if enabled else 'off'}")


@config_app.command("set")
def config_set(name: str, value: str):
    """开启/关闭运行时开关：on|off。"""
    v = value.strip().lower()
    if v in {"on", "true", "1", "yes", "enable"}:
        enabled = True
    elif v in {"off", "false", "0", "no", "disable"}:
        enabled = False
    else:
        raise typer.BadParameter("value 必须是 on 或 off")
    kb = _kb()
    try:
        now = kb.set_flag(name, enabled)
    except KBError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"{name}={'on' if now else 'off'}")


@study_app.command("enable")
def study_enable():
    """开启学习扩展（不自动回填历史）。"""
    kb = _kb()
    kb.set_flag("study.enabled", True)
    typer.echo("study.enabled=on")


@study_app.command("disable")
def study_disable():
    """关闭学习扩展（不删除数据）。"""
    kb = _kb()
    kb.set_flag("study.enabled", False)
    typer.echo("study.enabled=off")


# ================= 服务 =================
@app.command()
def serve(
    host: str = typer.Option(None),
    port: int = typer.Option(None),
):
    """启动 REST API 服务（uvicorn）。"""
    import uvicorn

    settings = Settings()
    host = host or settings.api_host
    port = port or settings.api_port
    from .api.server import create_app

    app_ = create_app(settings=settings)
    typer.echo(f"REST API: http://{host}:{port}  (docs: /docs)")
    uvicorn.run(app_, host=host, port=port)


@app.command()
def chat():
    """启动 Gradio 聊天界面。"""
    from .ui.chat import build_demo

    kb = _kb()
    demo = build_demo(kb)
    demo.launch(server_name="127.0.0.1", server_port=Settings().ui_port)


# ================= 评估 =================
@app.command()
def eval(
    dataset: str = typer.Option("eval/datasets/baseline.jsonl", help="评测集路径"),
    mode: str = typer.Option("full", help="retrieval | full"),
    top_k: int = typer.Option(None),
    collection: Optional[str] = typer.Option(None),
    no_rerank: bool = typer.Option(False),
    no_rewrite: bool = typer.Option(False),
    compare: Optional[str] = typer.Option(None, help="对比实验，如: rerank=on,off;top_k=3,5"),
    output: Optional[str] = typer.Option(None, help="报告输出路径（目录）"),
):
    """运行评估：输出 recall@k / accuracy / faithfulness 等指标。"""
    kb = _kb()
    items = load_dataset(dataset)
    if not items:
        typer.echo(f"评测集为空: {dataset}", err=True)
        raise typer.Exit(1)
    problems = validate(items, {d.doc_id for d in kb.list_documents()})
    if problems:
        typer.echo("评测集校验问题：", err=True)
        for p in problems[:20]:
            typer.echo(f"  - {p}", err=True)
        if len(problems) > 20:
            typer.echo(f"  ... 共 {len(problems)} 条", err=True)
        raise typer.Exit(1)

    evaluator = Evaluator(kb, kb.settings)
    out_dir = Path(output) if output else Path("eval/reports")
    out_dir.mkdir(parents=True, exist_ok=True)

    if compare:
        param_sets = _parse_compare(compare)
        results = evaluator.compare(
            items, mode=mode, param_sets=param_sets, collection=collection
        )
    else:
        results = [
            evaluator.run(
                items, mode=mode, top_k=top_k,
                rerank=not no_rerank, rewrite=not no_rewrite,
                collection=collection,
            )
        ]
    md = compare_to_markdown(results)
    typer.echo(md)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    save_markdown(results, out_dir / f"report_{stamp}.md")
    for r in results:
        tag = "_".join(f"{k}{v}" for k, v in r.config.items() if k in ("top_k", "rerank", "rewrite"))
        save_json(r, out_dir / f"report_{stamp}_{tag}.json")
    typer.echo(f"\n报告已保存到 {out_dir}/")


@app.command(name="eval-build")
def eval_build(
    source_dir: str = typer.Argument(..., help="从中抽样的文档目录"),
    output: str = typer.Option("eval/datasets/baseline.jsonl", help="评测集输出路径"),
    samples: int = typer.Option(8, help="生成问答对数"),
    collection: Optional[str] = typer.Option(None),
):
    """LLM 辅助生成基线评测集（需人工抽检修正后使用）。"""
    kb = _kb()
    docs = kb.list_documents(collection)
    if not docs:
        typer.echo("知识库为空，请先 ingest", err=True)
        raise typer.Exit(1)
    sampled = docs[: samples * 3] if len(docs) > samples else docs
    items: list[EvalItem] = []
    for d in sampled[:samples]:
        prompt = (
            "基于下面这篇文档，生成 1 个知识问答评测对。\n"
            "要求：问题有明确答案、可检索验证；JSON 输出。\n"
            f"文档标题：{d.title}\n文档内容（截断）：\n{_parent_excerpt(d, 1500)}\n"
            "只输出 JSON：{\"query\": \"问题\", \"expected_answer\": \"参考答案\", \"category\": \"事实问答|概念解释|操作步骤|对比分析|综合\"}"
        )
        try:
            res = kb._llm.chat_json([{"role": "user", "content": prompt}])
            items.append(
                EvalItem(
                    query=res.get("query", ""),
                    expected_answer=res.get("expected_answer", ""),
                    relevant_doc_ids=[d.doc_id],
                    category=res.get("category", ""),
                )
            )
        except Exception as exc:
            typer.echo(f"跳过 {d.doc_id}: {exc}", err=True)
    save_dataset(items, output)
    typer.echo(f"已生成 {len(items)} 条评测对 → {output}")
    typer.echo("⚠️ 请人工抽检/修正后使用；relevant_doc_ids 已自动标注来源文档。")


def _parent_excerpt(doc, limit: int) -> str:
    """评测抽样用的正文窗口。绑定文档只在哈希一致时回读源文件。"""
    from .errors import IngestError
    from .ingestion.source_text import read_verified_source
    from .models import SOURCE_BOUND
    from .retrieval.source_window import slice_parent_window

    if doc.source_kind == SOURCE_BOUND and doc.source_path:
        try:
            cleaned = read_verified_source(doc.source_path, doc.content_hash or "")
        except (OSError, IngestError):
            cleaned = None
        if cleaned is None:
            return doc.summary
        return slice_parent_window(cleaned, start=0, end=limit, max_chars=limit)
    if doc.full_text:
        return doc.full_text[:limit]
    return doc.summary


def main() -> None:
    app()


if __name__ == "__main__":
    main()
