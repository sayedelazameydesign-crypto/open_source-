"""Knowledge tools — what an agent may do with the corpus, and what it may not.

``know_search`` is read-only and returns citations; there is deliberately **no**
"edit the corpus" tool. Documents are indexed by the operator (CLI, server, or a
sync job) so that an agent reasoning over a page cannot promote its own wording
into the reference set the next run will trust.
"""

from __future__ import annotations

from typing import Any

from os_core.types import RiskLevel

from os_know.retrieve import KnowRetriever
from os_know.store import KnowStore
from os_know.types import DocumentInput


def know_tools(store: KnowStore, retriever: KnowRetriever | None = None) -> list[Any]:
    """Build the ``know_*`` tools. *retriever* is optional so an index-only
    deployment (populate the corpus, query it from the API) is a legal state."""
    from os_tools.registry import Tool

    def _search(query: str, top_k: int = 5, corpus: str | None = None) -> dict[str, Any]:
        if retriever is None:
            return {"error": "no retriever configured", "count": 0, "hits": []}
        hits = retriever.query(query, corpus_ids=[corpus] if corpus else None, top_k=top_k)
        return {
            "count": len(hits),
            "hits": [
                {
                    "doc_id": hit.chunk.doc_id,
                    "heading": hit.chunk.heading,
                    "text": hit.chunk.text,
                    "citation": hit.chunk.citation(),
                    "untrusted_source": hit.chunk.untrusted,
                    "score": hit.score,
                }
                for hit in hits
            ],
        }

    def _list() -> dict[str, Any]:
        return {
            "corpora": [c.model_dump() for c in store.list_corpora()],
            "documents": [d.model_dump() for d in store.list_documents()],
        }

    tools = [
        Tool(
            name="know_search",
            description=(
                "Query the indexed document corpus. Returns quoted passages with "
                "citations (document, line span, content hash); passages indexed "
                "from a URL are flagged as untrusted."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "top_k": {"type": "integer", "minimum": 1},
                    "corpus": {"type": "string", "description": "restrict to one corpus id"},
                },
                "required": ["query"],
            },
            risk=RiskLevel.READ_ONLY,
            fn=_search,
        ),
        Tool(
            name="know_list",
            description="List indexed corpora and documents (ids, hashes, chunk counts).",
            parameters={"type": "object", "properties": {}, "required": []},
            risk=RiskLevel.READ_ONLY,
            fn=lambda: _list(),
        ),
    ]

    def _index(
        text: str, corpus_id: str = "default", title: str = "", source_uri: str | None = None
    ) -> dict:
        document = store.index_document(
            DocumentInput(text=text, corpus_id=corpus_id, title=title, source_uri=source_uri)
        )
        return document.model_dump()

    # Indexing is exposed as a *write* so HITL sees it; an agent that can add to
    # the corpus can steer every later answer drawn from it.
    tools.append(
        Tool(
            name="know_index",
            description=(
                "Index a document into a corpus. Requires approval: whatever is "
                "indexed here is quoted as reference in future runs."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "corpus_id": {"type": "string"},
                    "title": {"type": "string"},
                    "source_uri": {"type": "string"},
                },
                "required": ["text"],
            },
            risk=RiskLevel.WRITE,
            fn=_index,
        )
    )
    return tools


def register_know_tools(
    registry: Any,
    *,
    store: KnowStore,
    retriever: KnowRetriever | None = None,
) -> list[str]:
    """Add the knowledge tools to *registry* and report which names landed.

    Registration is explicit — called by the demo/server when
    ``CELA_KNOW_STORE_URL`` is set — so a corpus on disk does not silently become
    a capability.
    """
    tools = know_tools(store, retriever)
    registry.extend(tools)
    return [tool.name for tool in tools]


__all__ = ["know_tools", "register_know_tools"]
