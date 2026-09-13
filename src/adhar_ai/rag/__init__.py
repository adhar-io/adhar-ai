"""The platform's knowledge base (ADR-0024 §6).

`KnowledgeBase` is the one object callers need. It owns the pgvector store, the
in-process lexical index and the sources that populate them, and it exposes
three verbs: `refresh()` to re-derive knowledge from the platform, `search()` to
retrieve grounding, and `add_note()` / `learn_from_finding()` / `record_feedback()`
to take knowledge back in.
"""

from .documents import Chunk, Document, chunk_document, kind_weight
from .embeddings import EmbeddingBackend, GatewayEmbeddings, LocalEmbeddings, load_embeddings
from .knowledge import KnowledgeBase
from .lexical import LexicalIndex, tokenize
from .sources import (
    ClusterSource,
    DocsSource,
    FindingsSource,
    NotesSource,
    PackagesSource,
    Source,
    ToolsSource,
)
from .store import Hit, IngestReport, KnowledgeStore

__all__ = [
    "Chunk",
    "ClusterSource",
    "Document",
    "DocsSource",
    "EmbeddingBackend",
    "FindingsSource",
    "GatewayEmbeddings",
    "Hit",
    "IngestReport",
    "KnowledgeBase",
    "KnowledgeStore",
    "LexicalIndex",
    "LocalEmbeddings",
    "NotesSource",
    "PackagesSource",
    "Source",
    "ToolsSource",
    "chunk_document",
    "kind_weight",
    "load_embeddings",
    "tokenize",
]
