"use client";

import { useCallback, useEffect, useRef, useState } from "react";

type AgentEvent = {
  type:
    | "run_started"
    | "step_started"
    | "model_selected"
    | "thought"
    | "plan_created"
    | "tool_result"
    | "artifact"
    | "run_finished"
    | "error"
    | "approval_requested"
    | "approval_resolved";
  seq: number;
  step_id?: string | null;
  data: Record<string, unknown>;
};

type Snapshot = {
  run_id: string;
  status: string;
  mode?: string;
  task?: string;
  final_answer?: string;
  error?: string | null;
  events?: number;
  pending_approvals?: { id: string; name: string; args: Record<string, unknown> }[];
  steps?: { id: string; title: string; tool: string | null; status: string; attempts: number }[];
  artifacts?: { kind: string; filename: string; mime_type: string }[];
  models?: { model: string; role: string; reason: string }[];
  // Additional fields from live events
  budget?: {
    steps: number;
    steps_left: number;
    elapsed_s: number;
    tokens: number;
    consecutive_errors: number;
    most_repeated: string | null;
  };
  reason?: string;
};

const TERMINAL = new Set(["completed", "failed", "cancelled", "max_steps"]);
const ICONS: Record<string, string> = {
  thought: "💭",
  tool_call: "🔧",
  tool_result: "✓",
  model_selected: "🧠",
  plan_created: "🗺️",
  step_started: "▶",
  step_finished: "⏹",
  artifact: "📦",
  approval_requested: "🔐",
  approval_resolved: "🔓",
  review: "🧐",
  run_finished: "🏁",
  error: "⛔",
  warning: "⚠️",
  log: "ℹ️",
};

export default function Home() {
  const [task, setTask] = useState("Navigate to example.org and check the model list");
  const [mode, setMode] = useState("auto");
  const [runId, setRunId] = useState<string | null>(null);
  const [events, setEvents] = useState<AgentEvent[]>([]);
  const [snap, setSnap] = useState<Snapshot | null>(null);
  const [conn, setConn] = useState<"idle" | "live" | "resuming" | "closed">("idle");
  const [notice, setNotice] = useState("");
  const seqRef = useRef(0);
  const abortRef = useRef<AbortController | null>(null);
  const snapRef = useRef(snap);
  const snapshotInFlight = useRef(false);

  useEffect(() => {
    snapRef.current = snap;
  }, [snap]);

  // Fetch fresh snapshot from backend (polling)
  const refreshSnapshot = useCallback(async (id: string) => {
    const res = await fetch(`/api/backend/runs/${id}`);
    if (res.ok) setSnap((await res.json()) as Snapshot);
  }, []);

  // SSE event stream with after_seq resume
  const stream = useCallback(
    async (id: string) => {
      abortRef.current?.abort();
      const ctrl = new AbortController();
      abortRef.current = ctrl;
      // Resume from the exact sequence cursor — survives reloads too.
      const stored = Number(localStorage.getItem(`cela:lastSeq:${id}`) ?? 0);
      seqRef.current = Math.max(seqRef.current, Number.isFinite(stored) ? stored : 0);
      setConn(seqRef.current > 0 ? "resuming" : "live");
      try {
        const res = await fetch(`/api/backend/runs/${id}/events?after_seq=${seqRef.current}`, {
          signal: ctrl.signal,
        });
        if (!res.ok || !res.body) throw new Error(`stream HTTP ${res.status}`);
        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buf = "";
        for (;;) {
          const { done, value } = await reader.read();
          if (done) break;
          buf += decoder.decode(value, { stream: true });
          const chunks = buf.split("\n\n");
          buf = chunks.pop() ?? "";
          for (const chunk of chunks) {
            const line = chunk.split("\n").find((l) => l.startsWith("data: "));
            if (!line) continue;
            const event = JSON.parse(line.slice(6)) as AgentEvent;
            if (event.seq <= seqRef.current) continue; // exactly-once at the UI
            seqRef.current = event.seq;
            localStorage.setItem(`cela:lastSeq:${id}`, String(event.seq));
            setEvents((prev) => [...prev.slice(-400), event]);
            // Auto-refresh snapshot on terminal events
            if (
              event.type === "approval_requested" ||
              event.type === "approval_resolved" ||
              event.type === "run_finished"
            ) {
              void refreshSnapshot(id);
            }
            if (event.type === "run_finished") setConn("closed");
          }
        }
      } catch (e) {
        if (!ctrl.signal.aborted) setConn("closed");
        setNotice(`Connection error: ${e instanceof Error ? e.message : "unknown"}`);
      }
    },
    [refreshSnapshot],
  );

  async function start() {
    setNotice("");
    setEvents([]);
    seqRef.current = 0;
    const res = await fetch("/api/backend/runs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ task, mode, max_steps: 25 }),
    });
    if (!res.ok) {
      setNotice(`Failed to start run (${res.status}) — check server and token`);
      return;
    }
    const body = (await res.json()) as { run_id: string };
    setRunId(body.run_id);
    void refreshSnapshot(body.run_id);
    void stream(body.run_id);
  }

  // HITL approval
  async function approve(toolCallId: string, decision: "approved" | "denied") {
    if (!runId) return;
    const res = await fetch(`/api/backend/runs/${runId}/approve`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tool_call_id: toolCallId, decision }),
    });
    setNotice(res.ok ? `Decision sent: ${decision}` : `Failed (${res.status})`);
    void refreshSnapshot(runId);
  }

  const pending = snap?.pending_approvals ?? [];

  // Format budget display
  const formatBudget = (budget: any) => {
    if (!budget) return "-";
    const { steps, steps_left, elapsed_s, tokens, consecutive_errors, most_repeated } = budget;
    return `${steps} steps (${tokens} tokens, ${elapsed_s}s), errors: ${consecutive_errors}${most_repeated ? `, most repeated: ${most_repeated}` : ""}`;
  };

  // Render event as formatted line
  const renderEvent = (e: AgentEvent) => {
    const icon = ICONS[e.type] ?? "·";
    const seq = e.seq;

    switch (e.type) {
      case "run_started": {
        const task = (e.data.task || "").toString().slice(0, 60);
        const mode = (e.data.mode || "?").toString();
        return (
          <li key={`${seq}-${e.type}`}>
            <span className="seq">#{seq}</span>{" "}
            {icon} {e.type} — task: {task} — mode: {mode}
          </li>
        );
      }

      case "step_started": {
        const title = (e.data.title || "").toString().slice(0, 50);
        return (
          <li key={`${seq}-${e.type}`}>
            <span className="seq">#{seq}</span>{" "}
            {icon} {e.type} — {title}
          </li>
        );
      }

      case "model_selected": {
        const model = (e.data.model || "?").toString();
        const role = (e.data.role || "").toString();
        const reason = (e.data.reason || "").toString().slice(0, 80);
        return (
          <li key={`${seq}-${e.type}`}>
            <span className="seq">#{seq}</span>{" "}
            {icon} <b>Model:</b> {model} ({role}) — {reason}
          </li>
        );
      }

      case "thought": {
        const text = (e.data.text || "").toString().slice(0, 200);
        return (
          <li key={`${seq}-${e.type}`}>
            <span className="seq">#{seq}</span>{" "}
            {icon} Thought: {text}...
          </li>
        );
      }

      case "plan_created": {
        return (
          <li key={`${seq}-${e.type}`}>
            <span className="seq">#{seq}</span>{" "}
            {icon} <b>Plan created</b> with {((e.data.steps || []) as any[]).length} steps
          </li>
        );
      }

      case "tool_result": {
        const name = (e.data.name || "?").toString();
        const status = (e.data.status || "?").toString();
        const output = (e.data.output || "").toString().slice(0, 150);
        const statusColor = status === "success" ? "✓" : "✗";
        return (
          <li key={`${seq}-${e.type}`}>
            <span className="seq">#{seq}</span>{" "}
            {icon} <b>{name}</b> [{statusColor}] {output}...
          </li>
        );
      }

      case "artifact": {
        const filename = (e.data.filename || "unknown").toString();
        const kind = (e.data.kind || "?").toString();
        const mime = (e.data.mime_type || "?").toString();
        return (
          <li key={`${seq}-${e.type}`}>
            <span className="seq">#{seq}</span>{" "}
            {icon} <b>{filename}</b> ({kind} / {mime})
            {runId && (
              <a
                href={`/api/backend/artifacts/${runId}/${encodeURIComponent(filename)}`}
                target="_blank"
                rel="noreferrer"
                title="Open artifact"
                style={{ fontSize: "12px", marginLeft: "8px" }}
              >
                ↗
              </a>
            )}
          </li>
        );
      }

      case "run_finished": {
        const status = (e.data.status || "?").toString();
        const reason = (e.data.reason || "").toString().slice(0, 80);
        const steps = Number(e.data.steps ?? 0) || 0;
        const tokens = Number(e.data.tokens ?? 0) || 0;
        return (
          <li key={`${seq}-${e.type}`}>
            <span className="seq">#{seq}</span>{" "}
            {icon} <b>Run finished</b> — {status} after {steps} steps, {tokens} tokens — {reason}
          </li>
        );
      }

      case "error": {
        const text = (e.data.text || "").toString().slice(0, 150);
        return (
          <li key={`${seq}-${e.type}`}>
            <span className="seq">#{seq}</span>{" "}
            {icon} <b>Error:</b> {text}
          </li>
        );
      }

      default:
        return (
          <li key={`${seq}-${e.type}`}>
            <span className="seq">#{seq}</span>{" "}
            {icon} {e.type}
          </li>
        );
    }
  };

  // Snapshot is the source of truth (ADR-0002): poll it while a run is active.
  useEffect(() => {
    if (!runId) return;

    const tick = async () => {
      const currentSnapshot = snapRef.current;
      if (
        currentSnapshot?.run_id === runId &&
        TERMINAL.has(currentSnapshot.status)
      ) {
        return;
      }
      if (snapshotInFlight.current) return;

      snapshotInFlight.current = true;
      try {
        await refreshSnapshot(runId);
      } catch {
        // A later tick retries if the backend is temporarily unavailable.
      } finally {
        snapshotInFlight.current = false;
      }
    };

    const t = setInterval(() => void tick(), 1500);
    return () => clearInterval(t);
  }, [runId, refreshSnapshot]);

  const handleApprove = (toolCallId: string, decision: "approved" | "denied") => {
    approve(toolCallId, decision);
  };

  return (
    <main className="shell">
      <header>
        <h1>cela_use_browser <span>لوحة الوكيل الحية</span></h1>
        <p className={`conn conn-${conn}`}>
          {conn === "live" && "● Live stream (SSE via server proxy)"}
          {conn === "resuming" && `● Resuming from seq=${seqRef.current}`}
          {conn === "closed" && "○ Stream ended"}
          {conn === "idle" && "○ Waiting to run"}
          {runId && <> · <code>{runId}</code></>}
          {snap && <> · status: <b>{snap.status}</b></>}
        </p>
      </header>

      <section className="composer">
        <input value={task} onChange={(e) => setTask(e.target.value)} placeholder="Enter task…" />
        <select value={mode} onChange={(e) => setMode(e.target.value)}>
          <option value="auto">Auto</option>
          <option value="react">ReAct</option>
          <option value="flow">PlanningFlow</option>
        </select>
        <button onClick={() => void start()}>Run ▶</button>
        {runId && (
          <button className="ghost" onClick={() => void stream(runId)}>
            Reconnect stream
          </button>
        )}
      </section>

      {notice && <p className="notice">{notice}</p>}

      {pending.length > 0 && (
        <section className="approvals">
          <h2>🔐 Pending Human Approvals (HITL — no response = denied)</h2>
          {pending.map((p) => (
            <div key={p.id} className="approval">
              <code>{p.name}</code>
              <span>{JSON.stringify(p.args).slice(0, 160)}</span>
              <button onClick={() => approve(p.id, "approved")}>✓ Approve</button>
              <button className="deny" onClick={() => approve(p.id, "denied")}>✗ Deny</button>
            </div>
          ))}
        </section>
      )}

      <section className="split">
        <div className="pane">
          <h2>Live Execution Events</h2>
          <ol className="events">
            {events.map((e) => renderEvent(e))}
            {events.length === 0 && <li className="empty">No events yet — start a run.</li>}
          </ol>

          {snap?.steps && snap.steps.length > 0 && (
            <>
              <h2>Planned Steps (DAG)</h2>
              <ul className="steps">
                {snap.steps.map((s) => (
                  <li key={s.id}>
                    <code>{s.id}</code> {s.title} — <b>{s.status}</b>
                  </li>
                ))}
              </ul>
            </>
          )}

          {snap?.final_answer && (
            <>
              <h2>Final Answer</h2>
              <p className="answer">{snap.final_answer}</p>
            </>
          )}

          {snap?.error && <p className="notice">Error: {snap.error}</p>}
        </div>

        <div className="pane">
          <h2>Artifacts</h2>
          {(snap?.artifacts ?? []).length === 0 && <p className="empty">No outputs yet.</p>}
          <ul className="artifacts">
            {(snap?.artifacts ?? []).map((a) => (
              <li key={a.filename}>
                <span>📦 <b>{a.filename}</b></span>
                <small>{a.kind} · {a.mime_type}</small>
                {runId && (
                  <a
                    href={`/api/backend/artifacts/${runId}/${encodeURIComponent(a.filename)}`}
                    target="_blank"
                    rel="noreferrer"
                  >
                    Open data ↗
                  </a>
                )}
              </li>
            ))}
          </ul>

          <h2>Model Routing</h2>
          <ul className="steps">
            {(snap?.models ?? []).map((m, i) => (
              <li key={i}>
                🧠 <code>{m.model}</code> ({m.role}) — {m.reason}
              </li>
            ))}
            {(snap?.models ?? []).length === 0 && <li className="empty">—</li>}
          </ul>

          {/* Budget summary from last run */}
          {snap?.budget && (
            <div className="notice" style={{ marginTop: "12px", padding: "8px", background: "#2a1414" }}>
              <strong>Budget:</strong> {formatBudget(snap.budget)}
            </div>
          )}

          {snap?.reason && (
            <div className="notice" style={{ marginTop: "8px", fontSize: "12px", color: "#8b93a7" }}>
              {snap.reason}
            </div>
          )}
        </div>
      </section>
    </main>
  );
}