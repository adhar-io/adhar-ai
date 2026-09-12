"""pgvector RAG over the platform's docs, ADRs and runbooks (ADR-0024 §6)."""

from .embeddings import EmbeddingBackend, GatewayEmbeddings, LocalEmbeddings, load_embeddings
from .index import Chunk, chunk_markdown, ingest_path
from .retriever import Retriever

__all__ = [
    "Chunk",
    "EmbeddingBackend",
    "GatewayEmbeddings",
    "LocalEmbeddings",
    "Retriever",
    "chunk_markdown",
    "ingest_path",
    "load_embeddings",
]
