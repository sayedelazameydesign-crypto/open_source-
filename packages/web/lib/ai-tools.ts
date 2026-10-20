/**
 * Content + pure helpers for the "AI Tools for Software Developers" page
 * (`app/ai-tools`). Kept dependency-free and side-effect-free so it can be
 * unit-tested with the same tsc + node:test harness as the other web libs
 * (see `tests/web/run-ai-tools.cjs`).
 *
 * Source of the copy: `docs/AI_TOOLS_FOR_DEVELOPERS.md`. Prices and market
 * figures are reproduced as listed there — always verify current pricing on
 * the vendor's site before purchasing.
 */

export type ToolCategory =
  | "Agentic Coding"
  | "Pair Programming"
  | "AI IDE"
  | "App Builder"
  | "UI Generation"
  | "Security";

export interface AiTool {
  id: string;
  name: string;
  /** Display label exactly as listed in the source, e.g. "Free / $20/mo". */
  priceLabel: string;
  /**
   * Paid-tier monthly price in USD used by the stack calculator.
   * `0` = has a usable free tier only in this listing, `null` = custom /
   * contact pricing (excluded from totals, shown separately).
   */
  monthlyUsd: number | null;
  hasFreeTier: boolean;
  category: ToolCategory;
  blurb: string;
  url: string;
  tags: string[];
  featured?: boolean;
}

export const AI_TOOLS: AiTool[] = [
  {
    id: "claude-code",
    name: "Claude Code",
    priceLabel: "$20/mo",
    monthlyUsd: 20,
    hasFreeTier: false,
    category: "Agentic Coding",
    blurb:
      "Command-line agentic coding. Reads your repo, writes code, runs tests, commits. The one that broke IBM.",
    url: "https://aiindigo.com/tool/claude",
    tags: ["cli", "agents", "terminal", "codebase scan"],
    featured: true,
  },
  {
    id: "github-copilot",
    name: "GitHub Copilot",
    priceLabel: "$10/mo",
    monthlyUsd: 10,
    hasFreeTier: false,
    category: "Pair Programming",
    blurb: "AI pair programmer embedded in VS Code. Autocomplete on steroids.",
    url: "https://aiindigo.com/tool/github-copilot",
    tags: ["vs code", "autocomplete", "inline"],
  },
  {
    id: "cursor",
    name: "Cursor",
    priceLabel: "Free / $20/mo",
    monthlyUsd: 20,
    hasFreeTier: true,
    category: "AI IDE",
    blurb:
      "AI-native code editor. Full codebase context. Tab to accept, Cmd+K to edit.",
    url: "https://aiindigo.com/tool/cursor",
    tags: ["editor", "ide", "codebase context"],
  },
  {
    id: "windsurf",
    name: "Windsurf",
    priceLabel: "Free / $15/mo",
    monthlyUsd: 15,
    hasFreeTier: true,
    category: "AI IDE",
    blurb: "Codeium's AI IDE. Deep codebase understanding with Cascade agents.",
    url: "https://aiindigo.com/tool/windsurf",
    tags: ["editor", "ide", "cascade", "agents"],
  },
  {
    id: "replit",
    name: "Replit",
    priceLabel: "Free / $25/mo",
    monthlyUsd: 25,
    hasFreeTier: true,
    category: "App Builder",
    blurb: "Code in browser. AI agent builds, deploys, and explains your app.",
    url: "https://aiindigo.com/tool/replit",
    tags: ["browser", "deploy", "agent"],
  },
  {
    id: "v0",
    name: "v0.dev",
    priceLabel: "Free / $20/mo",
    monthlyUsd: 20,
    hasFreeTier: true,
    category: "UI Generation",
    blurb: "Describe a UI → get production React code. By Vercel.",
    url: "https://aiindigo.com/tool/v0",
    tags: ["react", "ui", "vercel", "frontend"],
  },
  {
    id: "bolt-new",
    name: "Bolt.new",
    priceLabel: "Free / $20/mo",
    monthlyUsd: 20,
    hasFreeTier: true,
    category: "App Builder",
    blurb: "Full-stack app from a prompt. Deploys instantly.",
    url: "https://aiindigo.com/tool/bolt-new",
    tags: ["full-stack", "prompt", "deploy"],
  },
  {
    id: "snyk",
    name: "Snyk",
    priceLabel: "Free / Contact",
    monthlyUsd: null,
    hasFreeTier: true,
    category: "Security",
    blurb: "AI-powered security scanning for dependencies and code.",
    url: "https://aiindigo.com/tool/snyk",
    tags: ["security", "dependencies", "scanning"],
  },
];

export const TOOL_CATEGORIES: ("All" | ToolCategory)[] = [
  "All",
  "Agentic Coding",
  "Pair Programming",
  "AI IDE",
  "App Builder",
  "UI Generation",
  "Security",
];

export interface MarketImpact {
  label: string;
  detail: string;
  /** Negative percentage move, e.g. -13.2 */
  movePct: number;
}

export const MARKET_IMPACT: MarketImpact[] = [
  { label: "CrowdStrike", detail: "Security incumbent repriced", movePct: -8 },
  {
    label: "IBM",
    detail: "Worst day since 2000 — mainframe modernization at risk",
    movePct: -13.2,
  },
  {
    label: "IGV ETF",
    detail: "Software sector ETF drawdown",
    movePct: -30,
  },
];

export interface Comparison {
  id: string;
  title: string;
  before: { headline: string; cost: string; time: string };
  after: { headline: string; cost: string; time: string };
}

export const COMPARISONS: Comparison[] = [
  {
    id: "security-audit",
    title: "Security audit",
    before: {
      headline: "External consultant",
      cost: "$200/hr",
      time: "2 weeks",
    },
    after: {
      headline: "Claude Code scans the entire codebase",
      cost: "$20/mo",
      time: "Minutes",
    },
  },
  {
    id: "build-app",
    title: "Build an app",
    before: {
      headline: "Team of 3–5 developers",
      cost: "$150K+",
      time: "3 months",
    },
    after: {
      headline: "Claude Code + one developer",
      cost: "One salary + tools",
      time: "2–4 weeks",
    },
  },
  {
    id: "cobol",
    title: "COBOL modernization",
    before: {
      headline: "IBM modernization contract",
      cost: "$500K+",
      time: "12 months",
    },
    after: {
      headline: "Claude modernizes the codebase",
      cost: "Tooling + review time",
      time: "One quarter",
    },
  },
];

export interface LearningStep {
  id: string;
  title: string;
  body: string;
}

export const LEARNING_PATH: LearningStep[] = [
  {
    id: "real-project",
    title: "Use Claude Code on a real project",
    body: "Clone a repo you maintain. Let Claude Code scan it, fix bugs, write tests. Compare its output to what you'd write.",
  },
  {
    id: "workflow",
    title: "Build an AI-augmented dev workflow",
    body: "Cursor or Windsurf as your IDE. Claude Code for heavy lifts. GitHub Copilot for autocomplete. Snyk for security. Stack them.",
  },
  {
    id: "orchestrator",
    title: "Become the orchestrator",
    body: "The future developer doesn't write every line — they architect systems, review AI output, and ensure quality. The best developers will be 10x more productive. The rest will be replaced by those developers.",
  },
];

/* ---------------------------------------------------------------- helpers */

export type ToolSort = "featured" | "price-asc" | "name";

export function filterTools(
  tools: AiTool[],
  query: string,
  category: "All" | ToolCategory,
): AiTool[] {
  const q = query.trim().toLowerCase();
  return tools.filter((tool) => {
    if (category !== "All" && tool.category !== category) return false;
    if (!q) return true;
    const haystack = [
      tool.name,
      tool.blurb,
      tool.category,
      tool.priceLabel,
      ...tool.tags,
    ]
      .join(" ")
      .toLowerCase();
    return q.split(/\s+/).every((word) => haystack.includes(word));
  });
}

export function sortTools(tools: AiTool[], sort: ToolSort): AiTool[] {
  const copy = [...tools];
  if (sort === "name") {
    copy.sort((a, b) => a.name.localeCompare(b.name));
  } else if (sort === "price-asc") {
    copy.sort(
      (a, b) =>
        (a.monthlyUsd ?? Number.POSITIVE_INFINITY) -
        (b.monthlyUsd ?? Number.POSITIVE_INFINITY),
    );
  } else {
    copy.sort(
      (a, b) => Number(b.featured ?? false) - Number(a.featured ?? false),
    );
  }
  return copy;
}

export interface StackSummary {
  count: number;
  /** Sum of known paid-tier monthly prices. */
  monthlyUsd: number;
  /** Names of selected tools with custom/contact pricing (not in total). */
  customPriced: string[];
  /** Names of selected tools that also offer a free tier. */
  freeTierAvailable: string[];
}

export function summarizeStack(
  tools: AiTool[],
  selectedIds: ReadonlySet<string>,
): StackSummary {
  const selected = tools.filter((tool) => selectedIds.has(tool.id));
  return {
    count: selected.length,
    monthlyUsd: selected.reduce(
      (sum, tool) => sum + (tool.monthlyUsd ?? 0),
      0,
    ),
    customPriced: selected
      .filter((tool) => tool.monthlyUsd === null)
      .map((tool) => tool.name),
    freeTierAvailable: selected
      .filter((tool) => tool.hasFreeTier)
      .map((tool) => tool.name),
  };
}

/**
 * The recommended starting stack from step 2 of the learning path, priced
 * from AI_TOOLS: Claude Code $20 + Cursor $20 + Copilot $10 + Windsurf $15.
 */
export const RECOMMENDED_STACK_IDS = [
  "claude-code",
  "cursor",
  "github-copilot",
  "windsurf",
] as const;

export function recommendedStackMonthlyUsd(tools: AiTool[] = AI_TOOLS): number {
  return summarizeStack(tools, new Set(RECOMMENDED_STACK_IDS)).monthlyUsd;
}

export interface SavingsInput {
  developers: number;
  hourlyRateUsd: number;
  /** Planned duration of the project the "before" way. */
  monthsBefore: number;
  /** Monthly tooling spend the "after" way (defaults to recommended stack). */
  stackMonthlyUsd?: number;
}

export interface SavingsEstimate {
  beforeCostUsd: number;
  afterCostUsd: number;
  afterLaborUsd: number;
  afterToolsUsd: number;
  monthsAfter: number;
  savedUsd: number;
  savedPct: number;
  monthsSaved: number;
}

const HOURS_PER_MONTH = 160;
/** The source's app-build example: 3 months → 2–4 weeks (≈ 3× compression). */
export const TIME_COMPRESSION = 3;

/**
 * Illustrative estimate based on the source's own "build an app" example:
 * a small team over N months vs. one developer with an AI stack in roughly
 * a third of the time. It is a planning heuristic, not a quote.
 */
export function estimateSavings(input: SavingsInput): SavingsEstimate {
  const developers = Math.max(1, Math.round(input.developers));
  const hourly = Math.max(0, input.hourlyRateUsd);
  const monthsBefore = Math.max(0.5, input.monthsBefore);
  const stack = input.stackMonthlyUsd ?? recommendedStackMonthlyUsd();

  const beforeCost = developers * monthsBefore * HOURS_PER_MONTH * hourly;
  const monthsAfter = Math.max(0.5, monthsBefore / TIME_COMPRESSION);
  const afterLabor = 1 * monthsAfter * HOURS_PER_MONTH * hourly;
  const afterTools = stack * monthsAfter;
  const afterCost = afterLabor + afterTools;
  const saved = beforeCost - afterCost;

  return {
    beforeCostUsd: Math.round(beforeCost),
    afterCostUsd: Math.round(afterCost),
    afterLaborUsd: Math.round(afterLabor),
    afterToolsUsd: Math.round(afterTools),
    monthsAfter: Math.round(monthsAfter * 10) / 10,
    savedUsd: Math.round(saved),
    savedPct:
      beforeCost > 0 ? Math.round((saved / beforeCost) * 1000) / 10 : 0,
    monthsSaved: Math.round((monthsBefore - monthsAfter) * 10) / 10,
  };
}

export function formatUsd(value: number): string {
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 0,
  }).format(value);
}
