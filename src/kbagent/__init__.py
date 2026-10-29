"""kbagent - 个人知识库 RAG Agent。"""

from .config import Settings
from .document_store.base import DocumentStore
from .kb import KnowledgeBase
from .models import Answer, Chunk, Document, RetrievalHit, SourceRef
from .storage.filters import Filter, all_of, any_of, eq, ne
from .vectorstore.base import VectorStore

__version__ = "0.1.0"

__all__ = [
    "Answer",
    "Chunk",
    "Document",
    "DocumentStore",
    "Filter",
    "KnowledgeBase",
    "RetrievalHit",
    "Settings",
    "SourceRef",
    "VectorStore",
    "__version__",
    "all_of",
    "any_of",
    "eq",
    "ne",
]
