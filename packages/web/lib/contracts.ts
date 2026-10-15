import {
  parseExecution,
  type ExecutionInfo,
} from "../components/execution-disclosure";

export type AgentEvent = {
  type: string;
  seq: number;
  step_id?: string | null;
  data: Record<string, unknown>;
};
export type PlanStep = {
  id: string;
  title: string;
  tool: string | null;
  status: string;
  attempts: number;
};
export type Artifact = { kind: string; filename: string; mime_type: string };
export type Approval = {
  id: string;
  name: string;
  args: Record<string, unknown>;
};
export type Snapshot = {
  run_id: string;
  status: string;
  execution: ExecutionInfo;
  task?: string;
  mode?: string;
  final_answer?: string;
  error?: string | null;
  steps?: PlanStep[];
  artifacts?: Artifact[];
  models?: { model: string; role: string; reason: string }[];
  pending_approvals?: Approval[];
  recorded_tool_results?: number;
  tool_results_summary?: {
    recorded: number;
    successful: number;
    unsuccessful: number;
  };
};
export const TERMINAL = new Set([
  "completed",
  "failed",
  "cancelled",
  "max_steps",
]);
const RUN_STATUSES = new Set([
  "pending",
  "running",
  "awaiting_approval",
  ...TERMINAL,
]);
export function object(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}
function invalid(): never {
  throw new Error("invalid_api_response");
}
export function parseSnapshot(value: unknown, id: string): Snapshot {
  if (
    !object(value) ||
    value.run_id !== id ||
    typeof value.status !== "string" ||
    !RUN_STATUSES.has(value.status)
  )
    invalid();
  for (const key of ["task", "mode", "final_answer"])
    if (value[key] !== undefined && typeof value[key] !== "string") invalid();
  if (
    value.error !== undefined &&
    value.error !== null &&
    typeof value.error !== "string"
  )
    invalid();
  const arrays = {
    steps: (v: Record<string, unknown>) =>
      ["id", "title", "status"].every((k) => typeof v[k] === "string") &&
      (v.tool === null || typeof v.tool === "string"),
    artifacts: (v: Record<string, unknown>) =>
      ["kind", "filename", "mime_type"].every((k) => typeof v[k] === "string"),
    models: (v: Record<string, unknown>) =>
      ["model", "role", "reason"].every((k) => typeof v[k] === "string"),
    pending_approvals: (v: Record<string, unknown>) =>
      typeof v.id === "string" && typeof v.name === "string" && object(v.args),
  };
  for (const [key, validate] of Object.entries(arrays)) {
    const list = value[key];
    if (
      list !== undefined &&
      (!Array.isArray(list) || !list.every((v) => object(v) && validate(v)))
    )
      invalid();
  }
  if (
    value.recorded_tool_results !== undefined &&
    (!Number.isInteger(value.recorded_tool_results) ||
      Number(value.recorded_tool_results) < 0)
  )
    invalid();
  if (value.tool_results_summary !== undefined) {
    const summary = value.tool_results_summary;
    if (
      !object(summary) ||
      !["recorded", "successful", "unsuccessful"].every(
        (k) => Number.isInteger(summary[k]) && Number(summary[k]) >= 0,
      )
    )
      invalid();
  }
  return { ...value, execution: parseExecution(value.execution) } as Snapshot;
}
export function parseEvent(value: unknown): AgentEvent {
  if (
    !object(value) ||
    typeof value.type !== "string" ||
    !Number.isInteger(value.seq) ||
    Number(value.seq) < 1 ||
    !object(value.data)
  )
    invalid();
  return value as AgentEvent;
}
