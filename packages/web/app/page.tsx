"use client";

import { useCallback, useEffect, useRef, useState } from "react";

type AgentEvent = {
  type: string;
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
};

const TERMINAL = new Set(["completed", "failed", "cancelled", "max_steps"]);
const ICONS: Record<string, string> = {
  thought: "💭", tool_call: "🔧", tool_result: "✓", model_selected: "🧠",
  plan_created: "🗺️", step_started: "▶", step_finished: "⏹", artifact: "📦",
  approval_requested: "🔐", approval_resolved: "🔓", review: "🧐",
  run_finished: "🏁", error: "⛔", warning: "⚠️", log: "ℹ️",
};

export default function Home() {
  const [task, setTask] = useState("Sum the integers from 0 to 100");
  const [mode, setMode] = useState("auto");
  const [runId, setRunId] = useState<string | null>(null);
  const [events, setEvents] = useState<AgentEvent[]>([]);
  const [snap, setSnap] = useState<Snapshot | null>(null);
  const [conn, setConn] = useState<"idle" | "live" | "resuming" | "closed">("idle");
  const [notice, setNotice] = useState("");
  const seqRef = useRef(0);
  const abortRef = useRef<AbortController | null>(null);

  const refreshSnapshot = useCallback(async (id: string) => {
    const res = await fetch(`/api/backend/runs/${id}`);
    if (res.ok) setSnap((await res.json()) as Snapshot);
  }, []);

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
            if (event.type === "approval_requested" || event.type === "run_finished") {
              void refreshSnapshot(id);
            }
            if (event.type === "run_finished") setConn("closed");
          }
        }
      } catch {
        if (!ctrl.signal.aborted) setConn("closed");
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
      setNotice(`تعذّر بدء التشغيل (${res.status}) — تحقق من تشغيل الخادم والرمز من جهة الخادم.`);
      return;
    }
    const body = (await res.json()) as { run_id: string };
    setRunId(body.run_id);
    void refreshSnapshot(body.run_id);
    void stream(body.run_id);
  }

  async function approve(toolCallId: string, decision: "approved" | "denied") {
    if (!runId) return;
    // HITL is fail-closed on the server: no answer within the timeout = denied.
    const res = await fetch(`/api/backend/runs/${runId}/approve`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tool_call_id: toolCallId, decision }),
    });
    setNotice(res.ok ? `أُرسل القرار: ${decision}` : `فشل إرسال القرار (${res.status})`);
    void refreshSnapshot(runId);
  }

  // Snapshot is the source of truth (ADR-0002): poll it while a run is active.
  useEffect(() => {
    if (!runId || (snap && TERMINAL.has(snap.status))) return;
    const t = setInterval(() => void refreshSnapshot(runId), 1000);
    return () => clearInterval(t);
  }, [runId, snap, refreshSnapshot]);

  const pending = snap?.pending_approvals ?? [];

  return (
    <main className="shell">
      <header>
        <h1>cela_use_browser <span>لوحة الوكيل الحية</span></h1>
        <p className={`conn conn-${conn}`}>
          {conn === "live" && "● بث حي (SSE عبر وكيل الخادم)"}
          {conn === "resuming" && `● استئناف من seq=${seqRef.current}`}
          {conn === "closed" && "○ انتهى البث"}
          {conn === "idle" && "○ بانتظار تشغيل"}
          {runId && <> · <code>{runId}</code></>}
          {snap && <> · الحالة: <b>{snap.status}</b></>}
        </p>
      </header>

      <section className="composer">
        <input value={task} onChange={(e) => setTask(e.target.value)} placeholder="صف المهمة…" />
        <select value={mode} onChange={(e) => setMode(e.target.value)}>
          <option value="auto">تلقائي</option>
          <option value="react">ReAct</option>
          <option value="flow">PlanningFlow</option>
        </select>
        <button onClick={() => void start()}>تشغيل ▶</button>
        {runId && (
          <button className="ghost" onClick={() => void stream(runId)}>إعادة الاتصال بالبث</button>
        )}
      </section>
      {notice && <p className="notice">{notice}</p>}

      {pending.length > 0 && (
        <section className="approvals">
          <h2>🔐 موافقات بشرية معلّقة (HITL — عدم الرد = رفض)</h2>
          {pending.map((p) => (
            <div key={p.id} className="approval">
              <code>{p.name}</code>
              <span>{JSON.stringify(p.args).slice(0, 160)}</span>
              <button onClick={() => void approve(p.id, "approved")}>موافقة ✓</button>
              <button className="deny" onClick={() => void approve(p.id, "denied")}>رفض ✗</button>
            </div>
          ))}
        </section>
      )}

      <section className="split">
        <div className="pane">
          <h2>مسار التنفيذ الحي</h2>
          <ol className="events">
            {events.map((e) => (
              <li key={`${e.seq}-${e.type}`}>
                <span className="seq">#{e.seq}</span>
                <span>{ICONS[e.type] ?? "·"} {e.type}</span>
                <code>{JSON.stringify(e.data).slice(0, 220)}</code>
              </li>
            ))}
            {events.length === 0 && <li className="empty">لا أحداث بعد — ابدأ تشغيلًا.</li>}
          </ol>
          {snap?.steps && snap.steps.length > 0 && (
            <>
              <h2>خطوات الخطة (DAG)</h2>
              <ul className="steps">
                {snap.steps.map((s) => (
                  <li key={s.id}><code>{s.id}</code> {s.title} — <b>{s.status}</b></li>
                ))}
              </ul>
            </>
          )}
          {snap?.final_answer && (
            <>
              <h2>الإجابة النهائية</h2>
              <p className="answer">{snap.final_answer}</p>
            </>
          )}
          {snap?.error && <p className="notice">خطأ: {snap.error}</p>}
        </div>

        <div className="pane">
          <h2>Artifacts</h2>
          {(snap?.artifacts ?? []).length === 0 && <p className="empty">لا مخرجات بعد.</p>}
          <ul className="artifacts">
            {(snap?.artifacts ?? []).map((a) => (
              <li key={a.filename}>
                <span>📦 <b>{a.filename}</b></span>
                <small>{a.kind} · {a.mime_type}</small>
                {runId && (
                  <a href={`/api/backend/artifacts/${runId}/${encodeURIComponent(a.filename)}`} target="_blank" rel="noreferrer">
                    فتح البيانات الوصفية ↗
                  </a>
                )}
              </li>
            ))}
          </ul>
          <h2>توجيه النماذج</h2>
          <ul className="steps">
            {(snap?.models ?? []).map((m, i) => (
              <li key={i}>🧠 <code>{m.model}</code> ({m.role}) — {m.reason}</li>
            ))}
            {(snap?.models ?? []).length === 0 && <li className="empty">—</li>}
          </ul>
        </div>
      </section>
    </main>
  );
}
