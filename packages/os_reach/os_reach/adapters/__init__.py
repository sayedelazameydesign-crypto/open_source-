"""Adapters that bind external capabilities to the reach contract.

Today there is one: :mod:`os_reach.adapters.cli`, the gated bridge to host-side
upstream CLIs (Agent-Reach's world). The native tier lives in
:mod:`os_reach.native` because it needs no adapter at all — it is httpx and the
standard library.
"""
