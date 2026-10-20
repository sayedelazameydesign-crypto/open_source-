import { object } from "./contracts";

/**
 * Contracts for the platform console (`/launchpad`, `/devbox`, `/database`,
 * `/registry`).
 *
 * The four sections are *read-only views* over two existing backend
 * surfaces:
 *   - `GET /platform/info` — deployment, execution, sandbox, store and image
 *     facts collected from the live process (no secrets, no writes);
 *   - `GET /ledger?limit=N` — recorded run outcomes and the aggregate summary.
 *
 * Nothing in this file can deploy, pull an image, or touch a database. The
 * parsers reject malformed payloads instead of rendering placeholders that
 * could be mistaken for real platform state.
 */

export type ExecutionIdentity = {
  kind: string;
  model_name: string | null;
  goal_verification: string;
};

export type PlatformInfo = {
  status: string;
  product: string;
  version: string;
  generated_at: string;
  process: {
    started_at: string;
    uptime_seconds: number;
    python_version: string;
    python_implementation: string;
    platform: string;
    machine: string | null;
  };
  deployment: {
    mode: string;
    app_name: string;
    listen_port: number | null;
    region: string | null;
    public_url_configured: boolean;
  };
  execution: ExecutionIdentity;
  sandbox: {
    code_exec_mode: string;
    requested_backend: string | null;
    backend: string | null;
    configuration_error: string | null;
    executes_untrusted_code: boolean;
    isolation: string;
    network_isolation: string;
  };
  store: {
    backend: string;
    scope: string;
    durable: boolean;
    target: string | null;
    ledger: LedgerDescriptor;
  };
  registry: {
    configured: boolean;
    source: string | null;
    ignored_invalid_reference: boolean;
    reference: string | null;
    host: string | null;
    repository: string | null;
    tag: string | null;
    digest: string | null;
    build_sha: string | null;
    build_time: string | null;
  };
  tools: { count: number; registered: string[] };
};

export type LedgerDescriptor = {
  enabled: boolean;
  path?: string;
  schema_version?: number;
  entries?: number;
  corrupt_lines?: number;
};

export type LedgerEntry = {
  seq: number;
  run_id: string;
  task: string;
  mode: string;
  status: string;
  outcome: string;
  started_at: string | null;
  finished_at: string | null;
  duration_ms: number | null;
  steps_used: number;
  tool_calls: number;
  tool_failures: number;
  artifacts: string[];
  error: string | null;
  recorded_at: string;
};

export type LedgerSummary = {
  total: number;
  outcomes: Record<string, number>;
  modes: Record<string, number>;
  statuses: Record<string, number>;
  success_rate: number;
  totals: {
    steps_used: number;
    tokens_used: number;
    tool_calls: number;
    tool_failures: number;
    replans: number;
    reviews: number;
  };
  duration_ms: {
    count: number;
    mean: number;
    p50: number;
    p95: number;
    max: number;
  };
  first_recorded_at: string | null;
  last_recorded_at: string | null;
  corrupt_lines: number;
};

export type LedgerReport = {
  ledger: LedgerDescriptor;
  entries: LedgerEntry[];
  summary: LedgerSummary | null;
};

function invalid(): never {
  throw new Error("invalid_api_response");
}

function str(value: unknown): string {
  if (typeof value !== "string") invalid();
  return value;
}

function nullableStr(value: unknown): string | null {
  if (value === null || value === undefined) return null;
  return str(value);
}

function bool(value: unknown): boolean {
  if (typeof value !== "boolean") invalid();
  return value;
}

function num(value: unknown): number {
  if (typeof value !== "number" || !Number.isFinite(value)) invalid();
  return value;
}

function nullableNum(value: unknown): number | null {
  if (value === null || value === undefined) return null;
  return num(value);
}

function stringRecord(value: unknown): Record<string, number> {
  if (!object(value)) invalid();
  const out: Record<string, number> = {};
  for (const [key, item] of Object.entries(value)) out[key] = num(item);
  return out;
}

function parseDescriptor(value: unknown): LedgerDescriptor {
  if (!object(value)) invalid();
  return {
    enabled: bool(value.enabled),
    path: typeof value.path === "string" ? value.path : undefined,
    schema_version:
      typeof value.schema_version === "number" ? value.schema_version : undefined,
    entries: typeof value.entries === "number" ? value.entries : undefined,
    corrupt_lines:
      typeof value.corrupt_lines === "number" ? value.corrupt_lines : undefined,
  };
}

export function parsePlatformInfo(value: unknown): PlatformInfo {
  if (!object(value) || value.status !== "ok") invalid();
  const process = value.process;
  const deployment = value.deployment;
  const execution = value.execution;
  const sandbox = value.sandbox;
  const store = value.store;
  const registry = value.registry;
  const tools = value.tools;
  if (
    !object(process) ||
    !object(deployment) ||
    !object(execution) ||
    !object(sandbox) ||
    !object(store) ||
    !object(registry) ||
    !object(tools)
  )
    invalid();
  if (!Array.isArray(tools.registered)) invalid();
  return {
    status: "ok",
    product: str(value.product),
    version: str(value.version),
    generated_at: str(value.generated_at),
    process: {
      started_at: str(process.started_at),
      uptime_seconds: num(process.uptime_seconds),
      python_version: str(process.python_version),
      python_implementation: str(process.python_implementation),
      platform: str(process.platform),
      machine: nullableStr(process.machine),
    },
    deployment: {
      mode: str(deployment.mode),
      app_name: str(deployment.app_name),
      listen_port: nullableNum(deployment.listen_port),
      region: nullableStr(deployment.region),
      public_url_configured: bool(deployment.public_url_configured),
    },
    execution: {
      kind: str(execution.kind),
      model_name: nullableStr(execution.model_name),
      goal_verification: str(execution.goal_verification),
    },
    sandbox: {
      code_exec_mode: str(sandbox.code_exec_mode),
      requested_backend: nullableStr(sandbox.requested_backend),
      backend: nullableStr(sandbox.backend),
      configuration_error: nullableStr(sandbox.configuration_error),
      executes_untrusted_code: bool(sandbox.executes_untrusted_code),
      isolation: str(sandbox.isolation),
      network_isolation: str(sandbox.network_isolation),
    },
    store: {
      backend: str(store.backend),
      scope: str(store.scope),
      durable: bool(store.durable),
      target: nullableStr(store.target),
      ledger: parseDescriptor(store.ledger),
    },
    registry: {
      configured: bool(registry.configured),
      source: nullableStr(registry.source),
      ignored_invalid_reference: bool(registry.ignored_invalid_reference),
      reference: nullableStr(registry.reference),
      host: nullableStr(registry.host),
      repository: nullableStr(registry.repository),
      tag: nullableStr(registry.tag),
      digest: nullableStr(registry.digest),
      build_sha: nullableStr(registry.build_sha),
      build_time: nullableStr(registry.build_time),
    },
    tools: {
      count: num(tools.count),
      registered: tools.registered.map((item) => str(item)),
    },
  };
}

function parseEntry(value: unknown): LedgerEntry {
  if (!object(value)) invalid();
  if (!Array.isArray(value.artifacts)) invalid();
  return {
    seq: num(value.seq),
    run_id: str(value.run_id),
    task: str(value.task),
    mode: str(value.mode),
    status: str(value.status),
    outcome: str(value.outcome),
    started_at: nullableStr(value.started_at),
    finished_at: nullableStr(value.finished_at),
    duration_ms: nullableNum(value.duration_ms),
    steps_used: num(value.steps_used),
    tool_calls: num(value.tool_calls),
    tool_failures: num(value.tool_failures),
    artifacts: value.artifacts.map((item) => str(item)),
    error: nullableStr(value.error),
    recorded_at: str(value.recorded_at),
  };
}

function parseSummary(value: unknown): LedgerSummary {
  if (!object(value)) invalid();
  if (!object(value.totals) || !object(value.duration_ms)) invalid();
  return {
    total: num(value.total),
    outcomes: stringRecord(value.outcomes),
    modes: stringRecord(value.modes),
    statuses: stringRecord(value.statuses),
    success_rate: num(value.success_rate),
    totals: {
      steps_used: num(value.totals.steps_used),
      tokens_used: num(value.totals.tokens_used),
      tool_calls: num(value.totals.tool_calls),
      tool_failures: num(value.totals.tool_failures),
      replans: num(value.totals.replans),
      reviews: num(value.totals.reviews),
    },
    duration_ms: {
      count: num(value.duration_ms.count),
      mean: num(value.duration_ms.mean),
      p50: num(value.duration_ms.p50),
      p95: num(value.duration_ms.p95),
      max: num(value.duration_ms.max),
    },
    first_recorded_at: nullableStr(value.first_recorded_at),
    last_recorded_at: nullableStr(value.last_recorded_at),
    corrupt_lines: num(value.corrupt_lines),
  };
}

export function parseLedgerReport(value: unknown): LedgerReport {
  if (!object(value) || !Array.isArray(value.entries)) invalid();
  return {
    ledger: parseDescriptor(value.ledger),
    entries: value.entries.map(parseEntry),
    summary:
      value.summary === null || value.summary === undefined
        ? null
        : parseSummary(value.summary),
  };
}

/** "3d 04:12:09" — a ceiling-free, purely descriptive uptime label. */
export function formatUptime(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return "غير معروف";
  const whole = Math.floor(seconds);
  const days = Math.floor(whole / 86400);
  const hours = Math.floor((whole % 86400) / 3600);
  const minutes = Math.floor((whole % 3600) / 60);
  const secs = whole % 60;
  const clock = [hours, minutes, secs]
    .map((part) => String(part).padStart(2, "0"))
    .join(":");
  return days > 0 ? `${days}d ${clock}` : clock;
}

export function formatDuration(ms: number | null): string {
  if (ms === null || !Number.isFinite(ms) || ms < 0) return "—";
  if (ms < 1000) return `${Math.round(ms)} ms`;
  return `${(ms / 1000).toFixed(1)} s`;
}

export function formatPercent(rate: number, digits = 1): string {
  if (!Number.isFinite(rate)) return "—";
  return `${(rate * 100).toFixed(digits)}%`;
}

/** True only for the two values the backend documents as real execution. */
export function executesRealCode(mode: string): boolean {
  return ["local", "e2b", "firecracker", "production"].includes(mode);
}
