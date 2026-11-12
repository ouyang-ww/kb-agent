"""本机 MCP：知识库组件的 stdio 调用面。"""

from .server import McpServer, serve_stdio

__all__ = ["McpServer", "serve_stdio"]
