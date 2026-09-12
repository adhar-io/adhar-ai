"""pgvector RAG over the platform's docs, ADRs and runbooks (ADR-0024 §6)."""

from .embeddings import EmbeddingBackend, GatewayEmbeddings, LocalEmbeddings, load_embeddings
from .index import Chunk, chunk_markdown, collect, ingest_path
from .lexical import LexicalIndex, tokenize
from .retriever import Retriever

__all__ = [
    "Chunk",
    "EmbeddingBackend",
    "GatewayEmbeddings",
    "LexicalIndex",
    "LocalEmbeddings",
    "Retriever",
    "chunk_markdown",
    "collect",
    "ingest_path",
    "load_embeddings",
    "tokenize",
]
