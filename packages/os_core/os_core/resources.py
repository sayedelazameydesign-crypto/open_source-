"""Local resource preflight — the gate in front of every local model launch.

A sub-agent that cannot fit in RAM does not fail politely: the OOM killer ends
the process mid-run and the orchestrator loses the whole trajectory. So the
sub-agent layer (ADR-0020) asks this module *before* it starts inference, the
same way the plan's Rust-side probes did — but in three tiers so the platform
picks up no new dependency to do it:

1. ``/proc/meminfo`` on Linux — the kernel's own accounting, zero imports.
2. :func:`os.sysconf` elsewhere — POSIX page counts, still stdlib only.
3. ``psutil`` when a deployment *chose* to install it (the ``local`` extra) —
   richer, but never required; the first two tiers cover the CI and demo paths.

The plan's ``raise ResourceWarning`` is deliberately a typed error here instead:
``ResourceWarning`` is a warnings-module class, and a *gate* that decides
whether work may start must not share a channel with lints. ``check_local_resources``
keeps the plan's control flow — returns a verdict on success, raises on
shortage — with :class:`LocalResourceError` as the raised type.
"""

from __future__ import annotations

import os

from pydantic import BaseModel, Field

_GIB = 1024**3


class LocalResourceError(RuntimeError):
    """The machine cannot host the requested workload right now."""


class ResourceSnapshot(BaseModel):
    """One reading of the host. Every field is observable without a network."""

    source: str = "procfs"  # "procfs" | "sysconf" | "psutil"
    total_ram_gb: float = 0.0
    available_ram_gb: float = 0.0
    swap_free_gb: float | None = None
    cpu_count: int = Field(default=1, ge=0)
    load_avg_1m: float | None = None

    @property
    def used_ratio(self) -> float:
        if self.total_ram_gb <= 0:
            return 0.0
        return max(0.0, 1.0 - (self.available_ram_gb / self.total_ram_gb))


class ResourceCheck(BaseModel):
    """The verdict a caller gates on — reasons are for the operator's log."""

    ok: bool
    reasons: list[str] = Field(default_factory=list)
    required_ram_gb: float = 0.0
    required_cpus: int = 0
    snapshot: ResourceSnapshot

    def raise_for_shortage(self) -> ResourceCheck:
        if not self.ok:
            raise LocalResourceError("; ".join(self.reasons) or "insufficient local resources")
        return self


def _read_proc_meminfo() -> dict[str, int] | None:
    try:
        with open("/proc/meminfo", encoding="ascii") as handle:
            entries: dict[str, int] = {}
            for line in handle:
                key, _, rest = line.partition(":")
                parts = rest.split()
                if parts and parts[0].isdigit():
                    # Values are in kB; keep bytes to avoid unit drift later.
                    entries[key.strip()] = int(parts[0]) * 1024
            return entries or None
    except OSError:
        return None


def _snapshot_procfs() -> ResourceSnapshot | None:
    mem = _read_proc_meminfo()
    if not mem or "MemTotal" not in mem:
        return None
    total = mem["MemTotal"]
    # MemAvailable is what the kernel says a *new* workload may take — exactly
    # the number this gate needs. The Free+Buffers+Cached fallback is the
    # pre-3.14 approximation and may overstate, so it is a fallback, not a peer.
    available = mem.get("MemAvailable")
    if available is None:
        available = mem.get("MemFree", 0) + mem.get("Buffers", 0) + mem.get("Cached", 0)
    swap_free = mem.get("SwapFree")
    swap_total = mem.get("SwapTotal")
    if swap_total is not None and swap_total == 0:
        swap_free = 0
    load_1m: float | None
    try:
        load_1m = os.getloadavg()[0]
    except (OSError, AttributeError):
        load_1m = None
    return ResourceSnapshot(
        source="procfs",
        total_ram_gb=total / _GIB,
        available_ram_gb=available / _GIB,
        swap_free_gb=(swap_free / _GIB) if swap_free is not None else None,
        cpu_count=os.cpu_count() or 1,
        load_avg_1m=load_1m,
    )


def _snapshot_sysconf() -> ResourceSnapshot | None:
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
        avail_pages = os.sysconf("SC_AVPHYS_PAGES")
    except (OSError, ValueError, AttributeError):
        return None
    if pages <= 0 or page_size <= 0:
        return None
    load_1m: float | None
    try:
        load_1m = os.getloadavg()[0]
    except (OSError, AttributeError):
        load_1m = None
    return ResourceSnapshot(
        source="sysconf",
        total_ram_gb=(pages * page_size) / _GIB,
        available_ram_gb=(max(avail_pages, 0) * page_size) / _GIB,
        cpu_count=os.cpu_count() or 1,
        load_avg_1m=load_1m,
    )


def _snapshot_psutil() -> ResourceSnapshot | None:
    try:
        import psutil  # type: ignore[import-not-found]
    except ImportError:
        return None
    virtual = psutil.virtual_memory()
    swap = psutil.swap_memory()
    load_1m: float | None
    try:
        load_1m = os.getloadavg()[0]
    except (OSError, AttributeError):
        load_1m = None
    return ResourceSnapshot(
        source="psutil",
        total_ram_gb=virtual.total / _GIB,
        available_ram_gb=virtual.available / _GIB,
        swap_free_gb=swap.free / _GIB,
        cpu_count=os.cpu_count() or 1,
        load_avg_1m=load_1m,
    )


def snapshot_resources() -> ResourceSnapshot:
    """Read the host once. Procfs first — the cheapest honest source."""
    for probe in (_snapshot_procfs, _snapshot_sysconf, _snapshot_psutil):
        snap = probe()
        if snap is not None:
            return snap
    # A platform with none of the three still deserves an answer: zero RAM
    # reads as "unknown", and the gate below treats unknown totals as a fail
    # only when the caller demanded gigabytes we cannot prove exist.
    return ResourceSnapshot(source="sysconf", total_ram_gb=0.0, available_ram_gb=0.0)


def assess_resources(
    required_ram_gb: float = 4.0,
    required_cpus: int = 1,
    *,
    snapshot: ResourceSnapshot | None = None,
) -> ResourceCheck:
    """Pure assessment — never raises, so callers may log-and-continue."""
    snap = snapshot if snapshot is not None else snapshot_resources()
    reasons: list[str] = []
    if required_ram_gb > 0 and snap.available_ram_gb < required_ram_gb:
        reasons.append(
            f"available RAM {snap.available_ram_gb:.2f} GiB < required {required_ram_gb:.2f} GiB"
        )
    if required_cpus > 0 and snap.cpu_count < required_cpus:
        reasons.append(f"cpu count {snap.cpu_count} < required {required_cpus}")
    return ResourceCheck(
        ok=not reasons,
        reasons=reasons,
        required_ram_gb=required_ram_gb,
        required_cpus=required_cpus,
        snapshot=snap,
    )


def check_local_resources(
    required_ram_gb: float = 4.0,
    required_cpus: int = 1,
    *,
    snapshot: ResourceSnapshot | None = None,
) -> ResourceCheck:
    """Plan-shaped gate: verdict on success, :class:`LocalResourceError` on shortage.

    ``required_ram_gb=4`` is the plan's default for a small local model
    (Llama-3-8B quantised, DeepSeek-R1-Distill). Raise the requirement per
    sub-agent config rather than hoping the swap file saves the run.
    """
    return assess_resources(required_ram_gb, required_cpus, snapshot=snapshot).raise_for_shortage()


def format_snapshot(snapshot: ResourceSnapshot | None = None) -> str:
    """One operator-readable line — used by the CLI and failure messages."""
    snap = snapshot if snapshot is not None else snapshot_resources()
    load = f"{snap.load_avg_1m:.2f}" if snap.load_avg_1m is not None else "n/a"
    return (
        f"{snap.source}: RAM {snap.available_ram_gb:.2f}/{snap.total_ram_gb:.2f} GiB available, "
        f"cpus={snap.cpu_count}, load1m={load}"
    )


__all__ = [
    "LocalResourceError",
    "ResourceCheck",
    "ResourceSnapshot",
    "assess_resources",
    "check_local_resources",
    "format_snapshot",
    "snapshot_resources",
]
