"""kbagent - 个人知识库 RAG Agent。"""

from .config import Settings
from .kb import KnowledgeBase
from .models import Answer, Chunk, Document, RetrievalHit, SourceRef

__version__ = "0.1.0"

__all__ = [
    "Answer",
    "Chunk",
    "Document",
    "KnowledgeBase",
    "RetrievalHit",
    "Settings",
    "SourceRef",
    "__version__",
]
