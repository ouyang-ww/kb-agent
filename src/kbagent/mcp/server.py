"""本机 MCP 服务（stdio，换行分隔的 JSON-RPC）。

工具只有检索、问答，以及按文档 id 打开已绑定原文。
``study.enabled`` 打开时才列出 ``study``。不提供读文件、列目录或 shell。
对外结果默认去掉绝对 ``source_path`` / ``image_path``，与 REST 同一条规则。
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

from .. import __version__
from ..api.server import public_source_path
from ..errors import KBError, NotFoundError, StudyDisabledError
from ..models import MediaRef
from ..open_original import OriginalLaunch, open_bound_original

logger = logging.getLogger(__name__)

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
DEFAULT_PROTOCOL = "2024-11-05"
SERVER_NAME = "kbagent"

_PATH_KEYS = frozenset({"source_path", "image_path", "original_path"})
_SECRET_KEYS = frozenset(
    {
        "api_key",
        "llm_api_key",
        "api_read_key",
        "api_admin_key",
        "storage_dir",
        "documents_db",
        "chroma_dir",
        "study_db",
    }
)
_SEARCH_FIELDS = frozenset({"query", "top_k", "collection"})
_OPEN_FIELDS = frozenset({"doc_id"})
_STUDY_FIELDS = frozenset({"action", "item_id", "response", "confidence"})

_INSTRUCTIONS = (
    "kbagent 是本机知识库。search 检索，ask 问答，两者都只看默认可见范围，"
    "隐藏、private 和 secret 文档不会返回。响应默认不含绝对路径。"
    "open_original 只接受 doc_id，由本机用已绑定路径打开原文，优先 Obsidian，"
    "否则用系统默认应用；不要传路径。study 只在学习模块开启时存在。"
    "没有读文件、列目录或执行命令的工具。"
)


def _tool(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
    }


def _search_tool() -> dict:
    return _tool(
        "search",
        "语义检索知识库。只返回默认可见文档，不含隐藏、private 或 secret。"
        "结果里的绝对路径默认被去掉。",
        {
            "query": {"type": "string", "description": "检索问题"},
            "top_k": {
                "type": "integer",
                "minimum": 1,
                "maximum": 50,
                "description": "返回条数，默认与 KB_TOP_K 相同",
            },
            "collection": {"type": "string", "description": "只查这个集合"},
        },
        ["query"],
    )


def _ask_tool() -> dict:
    return _tool(
        "ask",
        "根据知识库回答问题，并给出引用。可见范围与默认 search 相同。"
        "来源里的绝对路径默认被去掉。",
        {
            "query": {"type": "string", "description": "问题"},
            "top_k": {"type": "integer", "minimum": 1, "maximum": 50},
            "collection": {"type": "string", "description": "只查这个集合"},
        },
        ["query"],
    )


def _open_tool() -> dict:
    return _tool(
        "open_original",
        "用本机已保存的绑定路径打开原文。优先 Obsidian，启动失败再用系统默认应用。"
        "只接受 doc_id。默认检索不会返回的文档（隐藏、过期、或不在允许的 scope 里）"
        "会失败，且不返回路径。没有 include-expired 旁路。",
        {"doc_id": {"type": "string", "description": "文档 id"}},
        ["doc_id"],
    )


def _study_tool() -> dict:
    return _tool(
        "study",
        "学习切片。action=today 列出今天到期的题（不含答案）；"
        "action=answer 提交作答。模块关闭时这个工具不出现。",
        {
            "action": {
                "type": "string",
                "enum": ["today", "answer"],
                "description": "today 或 answer",
            },
            "item_id": {"type": "string", "description": "answer 时的题目 id"},
            "response": {"type": "string", "description": "answer 时的作答"},
            "confidence": {
                "type": "integer",
                "minimum": 1,
                "maximum": 3,
                "description": "信心 1–3，只记录，不参与调度",
            },
        },
        ["action"],
    )


def redact_external(value: Any, expose: bool) -> Any:
    """去掉绝对路径和内部配置键。相对路径保留，规则与 REST 的 public_source_path 相同。"""
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            if key in _SECRET_KEYS:
                continue
            if key in _PATH_KEYS and isinstance(item, str):
                cleaned[key] = public_source_path(item, expose)
            else:
                cleaned[key] = redact_external(item, expose)
        return cleaned
    if isinstance(value, list):
        return [redact_external(item, expose) for item in value]
    return value


def _media_payload(media: MediaRef | None, expose: bool) -> dict | None:
    if media is None:
        return None
    return redact_external(
        {
            "media_type": media.media_type,
            "image_path": media.image_path,
            "page": media.page,
            "caption": media.caption,
        },
        expose,
    )


def _require_object(arguments: Any, allowed: frozenset[str]) -> dict:
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        raise KBError("工具参数必须是对象")
    extra = sorted(set(arguments) - allowed)
    if extra:
        raise KBError("不接受的参数: " + "、".join(extra))
    return arguments


def _required_text(data: dict, name: str) -> str:
    value = data.get(name)
    if not isinstance(value, str) or not value.strip():
        raise KBError(f"{name} 必须是非空字符串")
    return value.strip()


def _optional_text(data: dict, name: str) -> str | None:
    if name not in data or data[name] is None:
        return None
    value = data[name]
    if not isinstance(value, str) or not value.strip():
        raise KBError(f"{name} 必须是非空字符串")
    return value.strip()


def _optional_top_k(data: dict) -> int | None:
    if "top_k" not in data or data["top_k"] is None:
        return None
    value = data["top_k"]
    if isinstance(value, bool) or not isinstance(value, int):
        raise KBError("top_k 必须是 1 到 50 的整数")
    if value < 1 or value > 50:
        raise KBError("top_k 必须是 1 到 50 的整数")
    return value


class _ProtocolError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


class McpServer:
    """一个知识库上的 MCP 工具面。每次列工具都重新读 study.enabled。"""

    def __init__(self, kb, *, launch: OriginalLaunch | None = None) -> None:
        self.kb = kb
        self.launch = launch
        # None 表示还没向客户端宣布过工具列表。之后 study 开关变化才发通知。
        self._study_announced: bool | None = None

    def _expose(self) -> bool:
        return bool(self.kb.settings.expose_source_paths)

    def pop_tools_changed(self) -> dict | None:
        """study 开关变化时返回 MCP 的 tools/list_changed 通知。

        第一次调用只记住当前状态，不发通知。stdio 循环在每条请求前调用，
        这样客户端不用重连就能刷新工具列表。
        """
        enabled = bool(self.kb.flag("study.enabled"))
        previous = self._study_announced
        self._study_announced = enabled
        if previous is None or previous == enabled:
            return None
        return {"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}

    def tool_definitions(self) -> list[dict]:
        tools = [_search_tool(), _ask_tool(), _open_tool()]
        if self.kb.flag("study.enabled"):
            tools.append(_study_tool())
        return tools

    def call_tool(self, name: str, arguments: Any) -> Any:
        if name == "search":
            return self._search(arguments)
        if name == "ask":
            return self._ask(arguments)
        if name == "open_original":
            return self._open(arguments)
        if name == "study":
            return self._study(arguments)
        raise KBError(f"未知工具: {name}")

    def _search(self, arguments: Any) -> dict:
        data = _require_object(arguments, _SEARCH_FIELDS)
        query = _required_text(data, "query")
        hits = self.kb.search(
            query,
            top_k=_optional_top_k(data),
            collection=_optional_text(data, "collection"),
        )
        expose = self._expose()
        payload = {
            "hits": [
                {
                    "chunk_id": hit.chunk_id,
                    "doc_id": hit.doc_id,
                    "title": hit.title,
                    "source_path": hit.source_path,
                    "section": hit.section,
                    "score": round(float(hit.score), 4),
                    "snippet": hit.text[:300],
                    "media": _media_payload(hit.media, expose),
                    "note": hit.note or "",
                }
                for hit in hits
            ]
        }
        return redact_external(payload, expose)

    def _ask(self, arguments: Any) -> dict:
        data = _require_object(arguments, _SEARCH_FIELDS)
        query = _required_text(data, "query")
        answer = self.kb.ask(
            query,
            top_k=_optional_top_k(data),
            collection=_optional_text(data, "collection"),
        )
        expose = self._expose()
        payload = {
            "text": answer.text,
            "sources": [
                {
                    "doc_id": source.doc_id,
                    "title": source.title,
                    "source_path": source.source_path,
                    "score": round(float(source.score), 4),
                    "snippet": source.snippet,
                    "section": source.section,
                    "media": _media_payload(source.media, expose),
                    "note": source.note or "",
                }
                for source in answer.sources
            ],
            "meta": answer.meta,
        }
        return redact_external(payload, expose)

    def _open(self, arguments: Any) -> dict:
        data = _require_object(arguments, _OPEN_FIELDS)
        doc_id = _required_text(data, "doc_id")
        result = open_bound_original(self.kb, doc_id, launch=self.launch)
        return {
            "opened": True,
            "doc_id": result.doc_id,
            "title": result.title,
            "opener": result.opener,
        }

    def _study(self, arguments: Any) -> dict:
        data = _require_object(arguments, _STUDY_FIELDS)
        action = data.get("action")
        if action not in {"today", "answer"}:
            raise KBError("study.action 只能是 today 或 answer")
        # 关闭时门面会抛错，并且不会创建 study.sqlite3。
        if action == "today":
            payload: Any = {"items": self.kb.study.today()}
        else:
            item_id = _required_text(data, "item_id")
            response = data.get("response")
            if not isinstance(response, str):
                raise KBError("response 必须是字符串")
            confidence = data.get("confidence")
            if confidence is not None and (
                isinstance(confidence, bool) or confidence not in (1, 2, 3)
            ):
                raise KBError("confidence 只能是 1、2 或 3")
            payload = self.kb.study.answer(item_id, response, confidence=confidence)
        return redact_external(payload, self._expose())

    def handle(self, message: dict) -> dict | None:
        """处理一条 JSON-RPC。通知返回 None，调用方不要写响应。"""
        if not isinstance(message, dict):
            return _error(None, -32600, "Invalid Request")
        method = message.get("method")
        has_id = "id" in message
        msg_id = message.get("id")
        if not isinstance(method, str) or not method:
            if has_id:
                return _error(msg_id, -32600, "Invalid Request")
            return None
        params = message.get("params")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            if has_id:
                return _error(msg_id, -32602, "Invalid params")
            return None
        try:
            result = self._dispatch(method, params)
        except _ProtocolError as exc:
            if not has_id:
                return None
            return _error(msg_id, exc.code, str(exc))
        except (KBError, NotFoundError, StudyDisabledError) as exc:
            if not has_id:
                return None
            if method == "tools/call":
                return _result(msg_id, _tool_error(str(exc)))
            return _error(msg_id, -32603, str(exc))
        except Exception as exc:
            logger.error(
                "MCP 请求失败 method=%s error=%s", method, type(exc).__name__
            )
            if not has_id:
                return None
            if method == "tools/call":
                return _result(
                    msg_id, _tool_error(f"工具执行失败（{type(exc).__name__}）")
                )
            return _error(msg_id, -32603, "内部错误")
        if not has_id:
            return None
        return _result(msg_id, result)

    def _dispatch(self, method: str, params: dict) -> Any:
        if method == "initialize":
            return self._initialize(params)
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": self.tool_definitions()}
        if method == "tools/call":
            return self._tools_call(params)
        if method.startswith("notifications/"):
            return None
        raise _ProtocolError(-32601, "Method not found")

    def _initialize(self, params: dict) -> dict:
        requested = params.get("protocolVersion")
        if requested in PROTOCOL_VERSIONS:
            version = requested
        else:
            version = DEFAULT_PROTOCOL
        return {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": True}},
            "serverInfo": {"name": SERVER_NAME, "version": __version__},
            "instructions": _INSTRUCTIONS,
        }

    def _tools_call(self, params: dict) -> dict:
        name = params.get("name")
        if not isinstance(name, str) or not name.strip():
            raise KBError("工具名必须是非空字符串")
        try:
            payload = self.call_tool(name.strip(), params.get("arguments"))
        except (KBError, NotFoundError, StudyDisabledError) as exc:
            return _tool_error(str(exc))
        return {
            "content": [{"type": "text", "text": _dump(payload)}],
            "isError": False,
        }


def _tool_error(message: str) -> dict:
    return {
        "content": [{"type": "text", "text": message}],
        "isError": True,
    }


def _result(msg_id: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _error(msg_id: Any, code: int, message: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": msg_id,
        "error": {"code": code, "message": message},
    }


def _dump(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False)


def dispatch_line(server: McpServer, line: str) -> str | None:
    """解析一行并返回要写回的一行。通知和空行返回 None。"""
    text = line.strip()
    if not text:
        return None
    try:
        message = json.loads(text)
    except json.JSONDecodeError:
        return _dump(_error(None, -32700, "无法解析 JSON"))
    if isinstance(message, list):
        return _dump(_error(None, -32600, "不支持批量请求"))
    response = server.handle(message)
    if response is None:
        return None
    return _dump(response)


def serve_stdio(
    kb,
    *,
    stdin=None,
    stdout=None,
    launch: OriginalLaunch | None = None,
) -> None:
    """从 stdin 读 MCP 消息，把响应写到 stdout。日志不要写 stdout。"""
    inp = sys.stdin if stdin is None else stdin
    out = sys.stdout if stdout is None else stdout
    if stdin is None and hasattr(inp, "reconfigure"):
        try:
            inp.reconfigure(encoding="utf-8")
        except (OSError, ValueError):
            pass
    if stdout is None and hasattr(out, "reconfigure"):
        try:
            out.reconfigure(encoding="utf-8")
        except (OSError, ValueError):
            pass
    server = McpServer(kb, launch=launch)
    for line in inp:
        if not line.strip():
            continue
        changed = server.pop_tools_changed()
        if changed is not None:
            out.write(_dump(changed) + "\n")
            out.flush()
        response = dispatch_line(server, line)
        if response is None:
            continue
        out.write(response + "\n")
        out.flush()
