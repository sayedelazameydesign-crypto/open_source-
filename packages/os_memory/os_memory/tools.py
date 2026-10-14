"""Memory tools — the surface an agent sees, and what each one is allowed to do.

Risk levels are not decoration here: :class:`~os_core.guardrails.Guardrails`
sends ``DANGEROUS`` to HITL with ``always_approve``, so *deleting* a durable
memory is a human decision while *recalling* one is free. ``remember`` sits in
between: it is a write, it is screened by the same gate any caller must pass, and
an agent that wants to persist something does so under the same rules the server
does.
"""

from __future__ import annotations

from typing import Any

from os_core.types import RiskLevel

from os_memory.manager import MemoryManager
from os_memory.types import MemoryKind, Provenance

_STRING = {"type": "string"}


def memory_tools(manager: MemoryManager) -> list[Any]:
    """Build the ``memory_*`` tools over *one* manager instance."""
    from os_tools.registry import Tool

    def _remember(
        text: str,
        key: str = "",
        kind: str = MemoryKind.FACT.value,
        tags: list[str] | None = None,
        origin_uri: str | None = None,
    ) -> dict[str, Any]:
        record, decision = manager.remember(
            text,
            kind=kind,
            key=key,
            tags=tags or [],
            # An agent asking to store something is the *least* trusted author: it
            # may have learned the sentence from a fetched page. So tool-authored
            # memories are recorded as imports unless a human surface says otherwise.
            provenance=Provenance.IMPORT if origin_uri else Provenance.RUN,
            origin_uri=origin_uri,
        )
        if record is None:
            return {"stored": False, "reason": decision.reason}
        return {
            "stored": True,
            "memory_id": record.memory_id,
            "status": record.status.value,
            "note": decision.reason,
        }

    def _recall(query: str, top_k: int = 3) -> dict[str, Any]:
        hits = manager.recall(query, top_k=top_k)
        return {
            "count": len(hits),
            "hits": [
                {
                    "memory_id": h.record.memory_id,
                    "text": h.record.text,
                    "kind": h.record.kind.value,
                    "provenance": h.record.provenance.value,
                    "verified": h.record.trust.value == "verified",
                    "score": h.score,
                }
                for h in hits
            ],
        }

    def _forget(memory_id: str) -> dict[str, Any]:
        return {"forgotten": manager.forget(memory_id), "memory_id": memory_id}

    return [
        Tool(
            name="memory_remember",
            description=(
                "Store one durable fact, preference or note for future sessions. "
                "State a single proposition; the write is screened, redacted and length-capped."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "text": _STRING,
                    "key": {**_STRING, "description": "stable subject id, e.g. 'deploy.target'"},
                    "kind": {"type": "string", "enum": [k.value for k in MemoryKind]},
                    "tags": {"type": "array", "items": _STRING},
                    "origin_uri": {**_STRING, "description": "where the statement came from"},
                },
                "required": ["text"],
            },
            risk=RiskLevel.WRITE,
            fn=_remember,
        ),
        Tool(
            name="memory_recall",
            description="Search long-term memory for facts relevant to a query.",
            parameters={
                "type": "object",
                "properties": {"query": _STRING, "top_k": {"type": "integer", "minimum": 1}},
                "required": ["query"],
            },
            risk=RiskLevel.READ_ONLY,
            fn=_recall,
        ),
        Tool(
            name="memory_forget",
            description="Permanently delete one stored memory by id.",
            parameters={
                "type": "object",
                "properties": {"memory_id": _STRING},
                "required": ["memory_id"],
            },
            risk=RiskLevel.DANGEROUS,
            fn=_forget,
        ),
    ]


__all__ = ["memory_tools"]
