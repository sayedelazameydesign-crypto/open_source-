"""os_reach — content-channel layer (Agent-Reach contract, our policy).

The platform can read the open internet through channels that each declare
what they can handle and how healthy they are, while the *platform* keeps the
security semantics: egress policy before every hop, the SSRF floor underneath
it, credentials read from the environment at request time, and hard caps on
what comes back.

The tier-0 channels here are native (httpx + stdlib, no external CLI). The
optional Agent-Reach adapter (:mod:`os_reach.agent_reach`, phase 2) adds the
upstream project's 16-platform coverage behind the same contract and the same
policy — see ``docs/REACH_PLAN.md`` and ADR-0013.
"""

from os_reach.adapters.cli import (
    CLI_CHANNEL_DISABLED,
    CLI_CHANNEL_SANDBOXED,
    CLI_CHANNEL_SPECS,
    KNOWN_UPSTREAM_CHANNELS,
    AgentReachBridge,
    CLIChannelAdapter,
    CLILayer,
    CLIProcessResult,
    ReachResult,
    build_reach_layer,
)
from os_reach.audit import ReachAuditEntry, ReachAuditLog, query_fingerprint
from os_reach.contract import (
    ChannelHealth,
    ReachChannel,
    ReachReport,
    ReachState,
    ReachTier,
    parse_doctor_json,
)
from os_reach.native import (
    FeedEntry,
    FetchResult,
    GitHubChannel,
    ReachFetcher,
    RssChannel,
    WebChannel,
    html_to_text,
    native_channels,
    parse_feed,
)
from os_reach.registry import ReachRegistry, build_native_registry
from os_reach.security import (
    default_reach_policy,
    hosts_from_env,
    scrub_credentials,
    truncate,
)
from os_reach.tools import cli_reach_tools, reach_tools, register_reach_tools

__all__ = [
    "CLI_CHANNEL_DISABLED",
    "CLI_CHANNEL_SANDBOXED",
    "CLI_CHANNEL_SPECS",
    "KNOWN_UPSTREAM_CHANNELS",
    "AgentReachBridge",
    "CLIChannelAdapter",
    "CLILayer",
    "CLIProcessResult",
    "ChannelHealth",
    "FeedEntry",
    "FetchResult",
    "GitHubChannel",
    "ReachAuditEntry",
    "ReachAuditLog",
    "ReachChannel",
    "ReachFetcher",
    "ReachRegistry",
    "ReachReport",
    "ReachResult",
    "ReachState",
    "ReachTier",
    "RssChannel",
    "WebChannel",
    "build_native_registry",
    "build_reach_layer",
    "cli_reach_tools",
    "default_reach_policy",
    "hosts_from_env",
    "html_to_text",
    "native_channels",
    "parse_doctor_json",
    "parse_feed",
    "query_fingerprint",
    "reach_tools",
    "register_reach_tools",
    "scrub_credentials",
    "truncate",
]
