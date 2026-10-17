"""Local resource preflight (ADR-0020) — the gate before any local model launch."""

from __future__ import annotations

import pytest
from os_core.resources import (
    LocalResourceError,
    ResourceSnapshot,
    assess_resources,
    check_local_resources,
    format_snapshot,
    snapshot_resources,
)


def test_snapshot_reads_this_machine() -> None:
    snap = snapshot_resources()
    assert snap.source in {"procfs", "sysconf", "psutil"}
    assert snap.cpu_count >= 1
    # The suite is running, so the machine demonstrably has *some* memory.
    assert snap.total_ram_gb > 0
    assert 0.0 <= snap.available_ram_gb <= snap.total_ram_gb
    assert 0.0 <= snap.used_ratio <= 1.0


def test_assess_is_pure_and_reports_both_dimensions() -> None:
    snap = ResourceSnapshot(source="procfs", total_ram_gb=16.0, available_ram_gb=2.0, cpu_count=4)
    check = assess_resources(4.0, 8, snapshot=snap)
    assert not check.ok
    assert any("RAM" in r for r in check.reasons)
    assert any("cpu" in r for r in check.reasons)
    assert check.snapshot is snap


def test_check_passes_on_a_generous_machine() -> None:
    check = check_local_resources(0.001, 1)
    assert check.ok
    assert check.reasons == []


def test_check_raises_on_shortage() -> None:
    # 1024 GiB free RAM is not a thing on the test host; the gate must refuse
    # loudly rather than let a model launch into an OOM.
    with pytest.raises(LocalResourceError) as excinfo:
        check_local_resources(1024.0, 1)
    assert "RAM" in str(excinfo.value)


def test_unknown_totals_fail_only_when_gigabytes_are_required() -> None:
    blind = ResourceSnapshot(source="sysconf", total_ram_gb=0.0, available_ram_gb=0.0)
    assert assess_resources(0.0, 1, snapshot=blind).ok
    assert not assess_resources(4.0, 1, snapshot=blind).ok


def test_format_snapshot_is_one_line() -> None:
    line = format_snapshot()
    assert "RAM" in line and "cpus=" in line
    assert "\n" not in line
