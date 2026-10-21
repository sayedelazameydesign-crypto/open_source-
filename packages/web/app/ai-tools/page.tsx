"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import { Icon } from "../../components/icon";
import {
  AI_TOOLS,
  COMPARISONS,
  LEARNING_PATH,
  MARKET_IMPACT,
  RECOMMENDED_STACK_IDS,
  TOOL_CATEGORIES,
  estimateSavings,
  filterTools,
  formatUsd,
  sortTools,
  summarizeStack,
  type ToolCategory,
  type ToolSort,
} from "../../lib/ai-tools";
import { buildStackTaskPrompt, buildWorkspaceHref } from "../../lib/page-links";

const PATH_KEY = "cela:ai-tools:path";
const STACK_KEY = "cela:ai-tools:stack";

const TERMINAL_LINES: { text: string; kind: "cmd" | "ok" | "warn" | "dim" }[] = [
  { text: "$ claude scan ./repo --security --fix", kind: "cmd" },
  { text: "⠋ reading 1,284 files · building call graph…", kind: "dim" },
  { text: "✓ 0 critical · 3 high · 11 medium — patches proposed", kind: "ok" },
  { text: "⚠ legacy COBOL module flagged → modernization plan ready", kind: "warn" },
  { text: "✓ 214 tests written · 98.2% coverage on changed files", kind: "ok" },
  { text: "✓ committed feat/ai-security-pass in 4m 12s — was a 2-week audit", kind: "ok" },
  { text: "$ █", kind: "cmd" },
];

function usePersistedSet(key: string, initial: string[]) {
  const [set, setSet] = useState<ReadonlySet<string>>(() => new Set(initial));
  useEffect(() => {
    try {
      const raw = window.localStorage.getItem(key);
      if (raw) {
        const parsed: unknown = JSON.parse(raw);
        if (Array.isArray(parsed)) {
          setSet(new Set(parsed.filter((v) => typeof v === "string")));
        }
      }
    } catch {
      /* localStorage optional */
    }
  }, [key]);
  const toggle = (id: string) => {
    setSet((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      try {
        window.localStorage.setItem(key, JSON.stringify([...next]));
      } catch {
        /* localStorage optional */
      }
      return next;
    });
  };
  return [set, toggle] as const;
}

function Terminal() {
  const [visible, setVisible] = useState(1);
  const [run, setRun] = useState(0);
  useEffect(() => {
    setVisible(1);
    const timer = window.setInterval(() => {
      setVisible((v) => {
        if (v >= TERMINAL_LINES.length) {
          window.clearInterval(timer);
          return v;
        }
        return v + 1;
      });
    }, 650);
    return () => window.clearInterval(timer);
  }, [run]);
  return (
    <div className="ait-terminal" aria-label="Illustrative terminal session">
      <div className="ait-terminal-bar">
        <span aria-hidden="true">● ● ●</span>
        <span>developer@workstation — claude-code</span>
        <button
          type="button"
          className="ait-replay"
          onClick={() => setRun((r) => r + 1)}
        >
          <Icon name="refresh" width="13" height="13" /> Replay
        </button>
      </div>
      <pre aria-live="off">
        {TERMINAL_LINES.slice(0, visible).map((line, i) => (
          <span key={`${run}-${i}`} className={`ait-line ait-line-${line.kind}`}>
            {line.text}
            {"\n"}
          </span>
        ))}
      </pre>
    </div>
  );
}

export default function AiToolsPage() {
  const [era, setEra] = useState<"before" | "after">("after");
  const [query, setQuery] = useState("");
  const [category, setCategory] = useState<"All" | ToolCategory>("All");
  const [sort, setSort] = useState<ToolSort>("featured");
  const [developers, setDevelopers] = useState(4);
  const [hourlyRate, setHourlyRate] = useState(100);
  const [months, setMonths] = useState(3);
  const [copied, setCopied] = useState(false);
  const [stack, toggleStack] = usePersistedSet(STACK_KEY, [
    ...RECOMMENDED_STACK_IDS,
  ]);
  const [doneSteps, toggleStep] = usePersistedSet(PATH_KEY, []);

  const tools = useMemo(
    () => sortTools(filterTools(AI_TOOLS, query, category), sort),
    [query, category, sort],
  );
  const stackSummary = useMemo(
    () => summarizeStack(AI_TOOLS, stack),
    [stack],
  );
  const estimate = useMemo(
    () =>
      estimateSavings({
        developers,
        hourlyRateUsd: hourlyRate,
        monthsBefore: months,
        stackMonthlyUsd:
          stackSummary.monthlyUsd > 0 ? stackSummary.monthlyUsd : undefined,
      }),
    [developers, hourlyRate, months, stackSummary.monthlyUsd],
  );
  const pathPct = Math.round((doneSteps.size / LEARNING_PATH.length) * 100);
  const maxDrop = Math.max(...MARKET_IMPACT.map((m) => Math.abs(m.movePct)));

  const copyStack = async () => {
    const lines = AI_TOOLS.filter((t) => stack.has(t.id)).map(
      (t) => `• ${t.name} — ${t.priceLabel} — ${t.url}`,
    );
    const text = [
      "My AI dev stack:",
      ...lines,
      `Total (known pricing): ${formatUsd(stackSummary.monthlyUsd)}/mo`,
      ...(stackSummary.customPriced.length
        ? [`Custom pricing: ${stackSummary.customPriced.join(", ")}`]
        : []),
    ].join("\n");
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2500);
    } catch {
      setCopied(false);
    }
  };

  return (
    <main className="ait" dir="ltr" lang="en">
      {/* ---------------------------------------------------------- hero */}
      <section className="ait-hero">
        <div className="ait-hero-copy">
          <span className="ait-eyebrow">
            AI Tools for Software Developers · Feb 2026
          </span>
          <h1>
            The tools changed.
            <br />
            The best developers <em>already adapted.</em>
          </h1>
          <p className="ait-lede">
            Between February 5–23, 2026, agentic coding repriced the software
            industry. Claude scans entire codebases for vulnerabilities
            overnight. Multi-agent teams write, review, and deploy code
            autonomously. The developer role isn&rsquo;t disappearing —
            it&rsquo;s transforming from writing code to orchestrating AI
            systems.
          </p>
          <div className="ait-hero-ctas">
            <a href="#tools" className="ait-btn ait-btn-primary">
              Explore the 8 tools <Icon name="arrow" width="15" height="15" />
            </a>
            <a href="#learning-path" className="ait-btn ait-btn-ghost">
              Your learning path
            </a>
            <Link href="/" className="ait-btn ait-btn-ghost">
              ← Workspace
            </Link>
          </div>
          <dl className="ait-hero-stats">
            <div>
              <dt>IGV software ETF</dt>
              <dd className="ait-down">−30%</dd>
            </div>
            <div>
              <dt>Security audit</dt>
              <dd>2 wks → minutes</dd>
            </div>
            <div>
              <dt>App build</dt>
              <dd>3 mo → 2–4 wks</dd>
            </div>
            <div>
              <dt>Entry cost</dt>
              <dd>$20/mo</dd>
            </div>
          </dl>
        </div>
        <Terminal />
      </section>

      {/* --------------------------------------------------- what happened */}
      <section className="ait-section" aria-labelledby="ait-what">
        <span className="ait-eyebrow">What happened</span>
        <h2 id="ait-what">
          Claude Code + Code Security + COBOL Modernization
        </h2>
        <p className="ait-section-lede">
          February 5–23, 2026. IBM&rsquo;s $200–500K mainframe modernization
          contracts compressed from years to quarters — and the market
          noticed before most engineering orgs did.
        </p>
        <div className="ait-impact">
          {MARKET_IMPACT.map((item) => (
            <div key={item.label} className="ait-impact-row">
              <div className="ait-impact-head">
                <strong>{item.label}</strong>
                <span className="ait-down">{item.movePct}%</span>
              </div>
              <div
                className="ait-impact-bar"
                role="img"
                aria-label={`${item.label} moved ${item.movePct} percent`}
              >
                <span
                  style={{
                    width: `${(Math.abs(item.movePct) / maxDrop) * 100}%`,
                  }}
                />
              </div>
              <small>{item.detail}</small>
            </div>
          ))}
        </div>
        <a
          className="ait-text-link"
          href="https://aiindigo.com/disruption-tracker"
          target="_blank"
          rel="noreferrer"
        >
          See full AI disruption timeline →
        </a>
      </section>

      {/* -------------------------------------------------- before vs after */}
      <section className="ait-section" aria-labelledby="ait-compare">
        <span className="ait-eyebrow">Before vs After</span>
        <h2 id="ait-compare">Same work. Radically different math.</h2>
        <div
          className="ait-era-toggle"
          role="group"
          aria-label="Compare before and after AI tooling"
        >
          <button
            type="button"
            aria-pressed={era === "before"}
            className={era === "before" ? "ait-era-active" : ""}
            onClick={() => setEra("before")}
          >
            Before
          </button>
          <button
            type="button"
            aria-pressed={era === "after"}
            className={era === "after" ? "ait-era-active" : ""}
            onClick={() => setEra("after")}
          >
            After
          </button>
        </div>
        <div className="ait-compare-grid">
          {COMPARISONS.map((item) => (
            <article key={item.id} className={`ait-compare-card ait-${era}`}>
              <h3>{item.title}</h3>
              <p className="ait-compare-headline">{item[era].headline}</p>
              <dl>
                <div>
                  <dt>Cost</dt>
                  <dd>{item[era].cost}</dd>
                </div>
                <div>
                  <dt>Time</dt>
                  <dd>{item[era].time}</dd>
                </div>
              </dl>
              <small className="ait-compare-flip">
                {era === "after"
                  ? `Before: ${item.before.cost} · ${item.before.time}`
                  : `After: ${item.after.cost} · ${item.after.time}`}
              </small>
            </article>
          ))}
        </div>
      </section>

      {/* ----------------------------------------------------- calculator */}
      <section className="ait-section" aria-labelledby="ait-calc">
        <span className="ait-eyebrow">Try it</span>
        <h2 id="ait-calc">What would your next project cost?</h2>
        <p className="ait-section-lede">
          An illustrative estimate using the source&rsquo;s own example — a
          small team over months, vs. one developer with an AI stack in about
          a third of the time. Uses your selected stack below (
          {formatUsd(stackSummary.monthlyUsd)}/mo). A planning heuristic, not
          a quote.
        </p>
        <div className="ait-calc">
          <div className="ait-calc-inputs">
            <label>
              Team size (before): <strong>{developers} developers</strong>
              <input
                type="range"
                min={1}
                max={12}
                value={developers}
                onChange={(e) => setDevelopers(Number(e.target.value))}
              />
            </label>
            <label>
              Blended hourly rate: <strong>{formatUsd(hourlyRate)}/hr</strong>
              <input
                type="range"
                min={25}
                max={250}
                step={5}
                value={hourlyRate}
                onChange={(e) => setHourlyRate(Number(e.target.value))}
              />
            </label>
            <label>
              Timeline (before): <strong>{months} months</strong>
              <input
                type="range"
                min={1}
                max={12}
                value={months}
                onChange={(e) => setMonths(Number(e.target.value))}
              />
            </label>
          </div>
          <dl className="ait-calc-results" aria-live="polite">
            <div>
              <dt>Before</dt>
              <dd>{formatUsd(estimate.beforeCostUsd)}</dd>
              <small>
                {developers} devs × {months} mo
              </small>
            </div>
            <div>
              <dt>After</dt>
              <dd className="ait-good">{formatUsd(estimate.afterCostUsd)}</dd>
              <small>
                1 dev × {estimate.monthsAfter} mo +{" "}
                {formatUsd(estimate.afterToolsUsd)} tools
              </small>
            </div>
            <div>
              <dt>You keep</dt>
              <dd className="ait-good">{formatUsd(estimate.savedUsd)}</dd>
              <small>
                {estimate.savedPct}% less · {estimate.monthsSaved} months
                earlier
              </small>
            </div>
          </dl>
        </div>
      </section>

      {/* ----------------------------------------------------------- tools */}
      <section className="ait-section" id="tools" aria-labelledby="ait-tools">
        <span className="ait-eyebrow">Tools that help</span>
        <h2 id="ait-tools">Eight tools. Build your stack.</h2>
        <p className="ait-section-lede">
          Search, filter, and tick “Add to stack” to price your own setup.
          The recommended starting stack from the learning path is pre-loaded.
        </p>
        <div className="ait-tool-controls">
          <input
            type="search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search tools, e.g. security, react, cli…"
            aria-label="Search tools"
          />
          <select
            value={sort}
            onChange={(e) => setSort(e.target.value as ToolSort)}
            aria-label="Sort tools"
          >
            <option value="featured">Featured first</option>
            <option value="price-asc">Price: low to high</option>
            <option value="name">Name A–Z</option>
          </select>
        </div>
        <div
          className="ait-chips"
          role="group"
          aria-label="Filter by category"
        >
          {TOOL_CATEGORIES.map((cat) => (
            <button
              key={cat}
              type="button"
              aria-pressed={category === cat}
              className={category === cat ? "ait-chip-active" : ""}
              onClick={() => setCategory(cat)}
            >
              {cat}
            </button>
          ))}
        </div>
        <p className="ait-result-count" role="status">
          {tools.length} of {AI_TOOLS.length} tools
          {query ? ` matching “${query}”` : ""}
        </p>
        <div className="ait-tool-grid">
          {tools.map((tool) => {
            const inStack = stack.has(tool.id);
            return (
              <article
                key={tool.id}
                className={`ait-tool-card${tool.featured ? " ait-tool-featured" : ""}${inStack ? " ait-tool-instack" : ""}`}
              >
                <header>
                  <h3>{tool.name}</h3>
                  <span className="ait-price">{tool.priceLabel}</span>
                </header>
                <span className="ait-category">
                  {tool.category}
                  {tool.hasFreeTier ? " · free tier" : ""}
                </span>
                <p>{tool.blurb}</p>
                <footer>
                  <button
                    type="button"
                    aria-pressed={inStack}
                    className="ait-stack-btn"
                    onClick={() => toggleStack(tool.id)}
                  >
                    <Icon
                      name={inStack ? "check" : "plus"}
                      width="14"
                      height="14"
                    />
                    {inStack ? "In your stack" : "Add to stack"}
                  </button>
                  <a
                    href={tool.url}
                    target="_blank"
                    rel="noreferrer"
                    className="ait-text-link"
                  >
                    View tool →
                  </a>
                </footer>
              </article>
            );
          })}
        </div>
        {tools.length === 0 && (
          <p className="ait-empty">
            No tool matches that search. Try “ide”, “deploy”, or clear the
            category filter.
          </p>
        )}

        <aside className="ait-stack-bar" aria-label="Your selected stack">
          <div>
            <strong>Your stack · {stackSummary.count} tools</strong>
            <span>
              {formatUsd(stackSummary.monthlyUsd)}/mo
              {stackSummary.customPriced.length > 0 &&
                ` + custom pricing (${stackSummary.customPriced.join(", ")})`}
              {stackSummary.freeTierAvailable.length > 0 &&
                ` · free tiers: ${stackSummary.freeTierAvailable.join(", ")}`}
            </span>
          </div>
          <div style={{ display: "flex", gap: "8px", flexWrap: "wrap" }}>
            <button
              type="button"
              className="ait-btn ait-btn-ghost"
              onClick={copyStack}
              disabled={stackSummary.count === 0}
            >
              {copied ? "Copied ✓" : "Copy my stack"}
            </button>
            <Link
              href={buildWorkspaceHref({
                task: buildStackTaskPrompt(
                  AI_TOOLS.filter((t) => stack.has(t.id)).map((t) => t.name),
                  stackSummary.monthlyUsd,
                  stackSummary.customPriced,
                ),
                mode: "flow",
              })}
              className="ait-btn ait-btn-primary"
            >
              Open stack in Workspace →
            </Link>
          </div>
        </aside>
      </section>

      {/* -------------------------------------------------- learning path */}
      <section
        className="ait-section"
        id="learning-path"
        aria-labelledby="ait-path"
      >
        <span className="ait-eyebrow">Your learning path</span>
        <h2 id="ait-path">Three steps from coder to orchestrator</h2>
        <div className="ait-progress" role="status">
          <span>
            {doneSteps.size} of {LEARNING_PATH.length} steps done — {pathPct}%
          </span>
          <div className="ait-progress-bar" aria-hidden="true">
            <span style={{ width: `${pathPct}%` }} />
          </div>
        </div>
        <ol className="ait-path-list">
          {LEARNING_PATH.map((step, i) => {
            const done = doneSteps.has(step.id);
            return (
              <li key={step.id} className={done ? "ait-step-done" : ""}>
                <span className="ait-step-index" aria-hidden="true">
                  {done ? "✓" : i + 1}
                </span>
                <div>
                  <h3>{step.title}</h3>
                  <p>{step.body}</p>
                  <label className="ait-step-check">
                    <input
                      type="checkbox"
                      checked={done}
                      onChange={() => toggleStep(step.id)}
                    />
                    {done ? "Done — nice work" : "Mark as done"}
                  </label>
                </div>
              </li>
            );
          })}
        </ol>
        {pathPct === 100 && (
          <p className="ait-congrats" role="status">
            🎉 Path complete. You&rsquo;re not competing with AI anymore —
            you&rsquo;re directing it.
          </p>
        )}
      </section>

      {/* ------------------------------------------------------------- cta */}
      <section className="ait-cta">
        <h2>The rest will be replaced by those developers.</h2>
        <p>
          The future developer doesn&rsquo;t write every line — they architect
          systems, review AI output, and ensure quality.
        </p>
        <div className="ait-hero-ctas">
          <a
            href="https://openclawterrace.com/"
            target="_blank"
            rel="noreferrer"
            className="ait-btn ait-btn-primary"
          >
            Join others adapting →
          </a>
          <a href="#tools" className="ait-btn ait-btn-ghost">
            Back to the tools
          </a>
        </div>
        <small className="ait-disclaimer">
          Prices, dates, and market figures reproduced as listed in the source
          document (<code>docs/AI_TOOLS_FOR_DEVELOPERS.md</code>) — verify
          current pricing on each vendor&rsquo;s site. External links point to
          the source&rsquo;s tool directory. Calculator figures are
          illustrative estimates, not quotes.
        </small>
      </section>
    </main>
  );
}
