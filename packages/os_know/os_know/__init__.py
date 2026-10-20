"""os_know — a corpus you own, indexed and queried offline.

The retrieval *engine* already existed: :class:`~os_tools.local_ai.LocalAIEngine`
does hybrid lexical+conceptual search and is deterministic. What was missing is
everything around it — a place for documents to live past the process, a chunking
rule, provenance per chunk, and a number that says whether retrieval actually
works. That is this package.

LlamaIndex is the obvious dependency here and is deliberately **not** taken: what
is worth keeping from it is the pipeline shape (``reader → node → index →
retriever`` as four swappable stages), not 52k stars of surface area. All four
stages are now present: :mod:`os_know.reader` is the document reader (ADR-0020),
the chunkers are the node stage, :class:`SQLiteKnowStore` the index, and
:class:`KnowRetriever` the retriever.
"""

from os_know.chunking import (
    MAX_CHARS,
    MAX_TOKENS,
    chunk_code,
    chunk_for,
    chunk_markdown,
    chunk_python,
    chunk_text,
)
from os_know.metrics import evaluate, mrr, recall_at_k
from os_know.papers import (
    DEFAULT_PAPER_CORPUS,
    index_research_paper,
    index_research_paper_file,
    normalize_doi,
    read_research_paper,
)
from os_know.reader import index_path, mime_for, read_document
from os_know.retrieve import MIN_DENSE_SCORE, KnowRetriever
from os_know.store import (
    DEFAULT_REINDEX_BATCH_SIZE,
    InMemoryKnowStore,
    KnowStore,
    SQLiteKnowStore,
    know_store_from_url,
)
from os_know.tools import know_tools
from os_know.types import Chunk, Corpus, Document, DocumentInput, KnowHit

__all__ = [
    "DEFAULT_PAPER_CORPUS",
    "DEFAULT_REINDEX_BATCH_SIZE",
    "MAX_CHARS",
    "MAX_TOKENS",
    "MIN_DENSE_SCORE",
    "Chunk",
    "Corpus",
    "Document",
    "DocumentInput",
    "InMemoryKnowStore",
    "KnowHit",
    "KnowRetriever",
    "KnowStore",
    "SQLiteKnowStore",
    "chunk_code",
    "chunk_for",
    "chunk_markdown",
    "chunk_python",
    "chunk_text",
    "evaluate",
    "index_path",
    "index_research_paper",
    "index_research_paper_file",
    "know_store_from_url",
    "know_tools",
    "mime_for",
    "mrr",
    "normalize_doi",
    "read_document",
    "read_research_paper",
    "recall_at_k",
]
