import type { IconName } from "../components/icon";

/**
 * Canonical route table and cross-page URL parameter helpers for `packages/web`.
 *
 * Kept dependency-free and side-effect-free so the route table, URL builders,
 * and strict query-string parsers can be verified offline with `node:test`
 * (`npm run test:page-links`).
 *
 * Security invariants:
 * - Query parameters coming from `window.location.search` are untrusted input.
 * - `parseWorkspaceParams` only populates form state (`task`, `mode`) or opens a
 *   read-only run view (`run_id`) — pages MUST NOT auto-submit `POST /runs`
 *   solely because query parameters are present.
 * - `mode` is restricted to the explicit `ALLOWED_EXECUTION_MODES` allowlist.
 * - `run_id` is restricted to `/^run_[a-zA-Z0-9_-]{1,128}$/` on `/` and
 *   `/^doc_[0-9a-f]{32}$/` on `/documents`.
 */

export type NavItem = {
  readonly href: string;
  readonly label: string;
  readonly icon: IconName;
  readonly activeMarker?: boolean;
};

export const WORKSPACE_NAV_ITEMS: readonly NavItem[] = [
  { href: "/", label: "مساحة العمل", icon: "grid", activeMarker: true },
  { href: "/documents", label: "المستندات النصية", icon: "file" },
  { href: "/status", label: "حالة المنصة", icon: "shield" },
  { href: "/ai-tools", label: "أدوات المطورين", icon: "spark" },
];

export const CONSOLE_NAV_ITEMS: readonly NavItem[] = [
  { href: "/launchpad", label: "App Launchpad", icon: "grid" },
  { href: "/devbox", label: "Devbox", icon: "activity" },
  { href: "/database", label: "Database", icon: "folder" },
  { href: "/registry", label: "Image Registry", icon: "layers" },
  { href: "/launcher", label: "Craft App Suite", icon: "link" },
];

export const PAGE_TITLES: Readonly<Record<string, string>> = {
  "/": "مساحة العمل · Mission OS",
  "/documents": "المستندات النصية",
  "/status": "حالة المنصة الحية",
  "/ai-tools": "AI Tools for Developers",
  "/launchpad": "App Launchpad · التطبيق المنشور",
  "/devbox": "Devbox · بيئة التنفيذ",
  "/database": "Database · المخزن والسجل",
  "/registry": "Image Registry · صورة الحاوية",
  "/launcher": "Craft App Suite · كتالوج التطبيقات",
};

export const FALLBACK_PAGE_TITLE = "أدوات المشروع";

export function resolvePageTitle(pathname: string): string {
  return Object.hasOwn(PAGE_TITLES, pathname)
    ? PAGE_TITLES[pathname]
    : FALLBACK_PAGE_TITLE;
}

export const ALLOWED_EXECUTION_MODES = ["auto", "react", "flow"] as const;
export type WorkspaceMode = (typeof ALLOWED_EXECUTION_MODES)[number];

export const MAX_TASK_QUERY_LENGTH = 4_000;
export const AGENT_RUN_ID_PATTERN = /^run_[a-zA-Z0-9_-]{1,128}$/;
export const DOCUMENT_RUN_ID_PATTERN = /^doc_[0-9a-f]{32}$/;

export function isValidWorkspaceMode(value: unknown): value is WorkspaceMode {
  return (
    typeof value === "string" &&
    (ALLOWED_EXECUTION_MODES as readonly string[]).includes(value)
  );
}

export function isValidAgentRunId(value: unknown): value is string {
  return typeof value === "string" && AGENT_RUN_ID_PATTERN.test(value);
}

export function isValidDocumentRunId(value: unknown): value is string {
  return typeof value === "string" && DOCUMENT_RUN_ID_PATTERN.test(value);
}

function sanitizeTaskText(raw: string | null | undefined): string | null {
  if (typeof raw !== "string") return null;
  const cleaned = raw.replace(/\0/g, "").trim();
  if (!cleaned) return null;
  return cleaned.slice(0, MAX_TASK_QUERY_LENGTH);
}

export function buildWorkspaceHref(params: {
  runId?: string | null;
  task?: string | null;
  mode?: string | null;
}): string {
  const query = new URLSearchParams();
  if (isValidAgentRunId(params.runId)) {
    query.set("run_id", params.runId);
  }
  const task = sanitizeTaskText(params.task);
  if (task) query.set("task", task);
  if (isValidWorkspaceMode(params.mode)) query.set("mode", params.mode);
  const serialized = query.toString();
  return serialized ? `/?${serialized}` : "/";
}

export function parseWorkspaceParams(search: string): {
  runId: string | null;
  task: string | null;
  mode: WorkspaceMode | null;
} {
  const normalized = search.startsWith("?") ? search.slice(1) : search;
  const query = new URLSearchParams(normalized);
  const rawRunId = query.get("run_id");
  const runId = isValidAgentRunId(rawRunId) ? rawRunId : null;
  const task = sanitizeTaskText(query.get("task"));
  const rawMode = query.get("mode");
  const mode = isValidWorkspaceMode(rawMode) ? rawMode : null;
  return { runId, task, mode };
}

export function buildDocumentsHref(params: { runId?: string | null }): string {
  if (!isValidDocumentRunId(params.runId)) return "/documents";
  const query = new URLSearchParams({ run_id: params.runId });
  return `/documents?${query.toString()}`;
}

export function parseDocumentsParams(search: string): { runId: string | null } {
  const normalized = search.startsWith("?") ? search.slice(1) : search;
  const query = new URLSearchParams(normalized);
  const candidate = query.get("run_id");
  return { runId: isValidDocumentRunId(candidate) ? candidate : null };
}

export function buildStackTaskPrompt(
  toolNames: readonly string[],
  monthlyUsd: number,
  customPriced: readonly string[],
): string {
  if (toolNames.length === 0) {
    return "اقترح حزمة أدوات تطوير مناسبة لمشروعي مع خطة اعتماد تدريجية";
  }
  const pricingNote =
    customPriced.length > 0
      ? ` ($${monthlyUsd}/mo + تسعير مخصص لـ ${customPriced.join(", ")})`
      : ` ($${monthlyUsd}/mo)`;
  return `راجع حزمة أدوات التطوير المختارة (${toolNames.join(", ")})${pricingNote} وضع خطة عملية لدمجها في سير العمل`;
}
