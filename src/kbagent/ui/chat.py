"""Gradio 聊天界面：问答 + 显示意图/改写/引用来源/图片命中。"""

from __future__ import annotations

import gradio as gr

from ..kb import KnowledgeBase


def build_demo(kb: KnowledgeBase) -> gr.Blocks:
    def respond(message: str, history: list, info_box: str, src_box: str, img_list: list):
        if not message.strip():
            return "", history, info_box, src_box, img_list
        try:
            ans = kb.ask(message)
        except Exception as exc:
            return (
                "",
                history + [
                    {"role": "user", "content": message},
                    {"role": "assistant", "content": f"出错了: {exc}"},
                ],
                info_box,
                src_box,
                img_list,
            )

        meta = ans.meta
        info_text = (
            f"**意图**: {meta.get('intent', '-')}  \n"
            f"**改写后查询**: {meta.get('rewritten_query', '-')}  \n"
            f"**检索命中**: {meta.get('retrieved', 0)} 条 → 扩写 {meta.get('expanded', 0)} 篇"
        )
        src_lines = []
        images: list[str] = []
        for i, s in enumerate(ans.sources, 1):
            src_lines.append(
                f"**[{i}] {s.title}**（doc_id: `{s.doc_id}`，得分 {s.score:.3f}）\n"
                f"> {s.snippet[:200]}"
            )
            if s.media and s.media.image_path:
                images.append(s.media.image_path)
        src_text = "\n\n".join(src_lines) or "（无引用）"
        history = history + [
            {"role": "user", "content": message},
            {"role": "assistant", "content": ans.text},
        ]
        return "", history, info_text, src_text, images

    with gr.Blocks(title="个人知识库助手") as demo:
        gr.Markdown("# 📚 个人知识库助手\n问我知识库里的任何问题，回答会附上引用来源。")
        chatbot = gr.Chatbot(height=420)
        msg = gr.Textbox(
            placeholder="例如：什么是向量检索？", show_label=False, autofocus=True
        )
        with gr.Accordion("🔍 检索过程（意图 / 改写）", open=False):
            info = gr.Markdown("")
        with gr.Accordion("📎 引用来源", open=False):
            sources = gr.Markdown("")
        gallery = gr.Gallery(label="🖼️ 命中的图片", columns=4, height=180)

        msg.submit(
            respond,
            [msg, chatbot, info, sources, gallery],
            [msg, chatbot, info, sources, gallery],
        )
    return demo


if __name__ == "__main__":
    build_demo(KnowledgeBase()).launch()
