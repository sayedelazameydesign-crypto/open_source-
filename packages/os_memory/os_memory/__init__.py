"""os_memory — durable memory across sessions, with a gate at the door.

The layer answers one question no other layer in this monorepo answers: *what
should still be true tomorrow?* ``os_skills`` stores behaviour, ``RunLedger``
stores outcomes, ``RunStore`` stores live state — none of them stores a fact.

Everything public flows through :class:`MemoryManager`, and every write flows
through :class:`MemoryGate`. There is no "raw" save path on purpose: durable
memory is the one component whose bad input keeps working against you.
"""

from os_memory.distiller import distill_run, tool_trace
from os_memory.gate import GateDecision, MemoryGate
from os_memory.manager import MemoryManager
from os_memory.retriever import DENSE_WEIGHT, MIN_DENSE_SCORE, SURFACE_WEIGHT, MemoryRetriever
from os_memory.store import (
    InMemoryMemoryStore,
    MemoryStore,
    PostgresMemoryStore,
    SQLiteMemoryStore,
    memory_store_from_url,
)
from os_memory.tools import memory_tools
from os_memory.types import (
    DEFAULT_NAMESPACE,
    MEMORY_BOUNDARY,
    MemoryHit,
    MemoryKind,
    MemoryRecord,
    MemoryStatus,
    Provenance,
    Trust,
    normalize_namespace,
)

__all__ = [
    "DEFAULT_NAMESPACE",
    "DENSE_WEIGHT",
    "MEMORY_BOUNDARY",
    "MIN_DENSE_SCORE",
    "SURFACE_WEIGHT",
    "GateDecision",
    "InMemoryMemoryStore",
    "MemoryGate",
    "MemoryHit",
    "MemoryKind",
    "MemoryManager",
    "MemoryRecord",
    "MemoryRetriever",
    "MemoryStatus",
    "MemoryStore",
    "PostgresMemoryStore",
    "Provenance",
    "SQLiteMemoryStore",
    "Trust",
    "distill_run",
    "memory_store_from_url",
    "memory_tools",
    "normalize_namespace",
    "tool_trace",
]
