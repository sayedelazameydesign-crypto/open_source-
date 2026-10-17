"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  parseExecution,
  type ExecutionInfo,
} from "../components/execution-disclosure";
import {
  object,
  parseEvent,
  parseSnapshot,
  TERMINAL,
  type AgentEvent,
  type Snapshot,
} from "../lib/contracts";

export function useAgentRun() {
  const [runId, setRunId] = useState<string | null>(null);
  const [events, setEvents] = useState<AgentEvent[]>([]);
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [connection, setConnection] = useState<
    "idle" | "live" | "resuming" | "closed" | "error"
  >("idle");
  const [backend, setBackend] = useState<
    "checking" | "available" | "unavailable"
  >("checking");
  const [notice, setNotice] = useState("");
  const [starting, setStarting] = useState(false);
  const [approving, setApproving] = useState<string | null>(null);
  const [runtimeExecution, setRuntimeExecution] = useState<ExecutionInfo>(
    parseExecution(null),
  );
  const activeRun = useRef<string | null>(null);
  const snapshotVersion = useRef(0);
  const snapshotRef = useRef<Snapshot | null>(null);
  const snapshotsInFlight = useRef(new Set<string>());
  const startingRef = useRef(false);
  const approvingRef = useRef(false);
  const seq = useRef(0);
  const abort = useRef<AbortController | null>(null);
  const alive = useRef(true);
  const cursor = (id: string) => {
    try {
      const n = Number(sessionStorage.getItem(`cela:lastSeq:${id}`));
      return Number.isSafeInteger(n) && n > 0 ? n : 0;
    } catch {
      return 0;
    }
  };
  useEffect(() => {
    alive.current = true;
    const ctrl = new AbortController();
    (async () => {
      try {
        const response = await fetch("/api/backend/health", {
          signal: ctrl.signal,
        });
        const data: unknown = await response.json();
        if (!response.ok || !object(data) || data.status !== "ok")
          throw new Error();
        if (!ctrl.signal.aborted) setBackend("available");
        const meta = await fetch("/api/backend/runtime-info", {
          signal: ctrl.signal,
        });
        const info: unknown = meta.ok ? await meta.json() : null;
        if (!ctrl.signal.aborted)
          setRuntimeExecution(
            parseExecution(object(info) ? info.execution : null),
          );
      } catch {
        if (!ctrl.signal.aborted) setBackend("unavailable");
      }
    })();
    return () => {
      alive.current = false;
      ctrl.abort();
      abort.current?.abort();
    };
  }, []);
  const refreshSnapshot = useCallback(async (id: string) => {
    // Polling, SSE and approvals share this guard; a slow request must not overlap.
    if (snapshotsInFlight.current.has(id)) return;
    const inFlight = snapshotsInFlight.current;
    inFlight.add(id);
    const version = ++snapshotVersion.current;
    try {
      const response = await fetch(
        `/api/backend/runs/${encodeURIComponent(id)}`,
      );
      if (!response.ok) throw new Error();
      const data = parseSnapshot(await response.json(), id);
      if (
        alive.current &&
        activeRun.current === id &&
        version === snapshotVersion.current
      ) {
        snapshotRef.current = data;
        setSnapshot(data);
        setBackend("available");
      }
    } catch {
      if (
        alive.current &&
        activeRun.current === id &&
        version === snapshotVersion.current
      )
        setNotice(
          "تعذر تحديث حالة المهمة أو أن الاستجابة غير صالحة. لا نفترض نجاح التنفيذ من انقطاع الاتصال.",
        );
    } finally {
      inFlight.delete(id);
    }
  }, []);
  const stream = useCallback(
    async (id: string) => {
      abort.current?.abort();
      const ctrl = new AbortController();
      abort.current = ctrl;
      seq.current = Math.max(seq.current, cursor(id));
      setConnection(seq.current ? "resuming" : "live");
      try {
        const response = await fetch(
          `/api/backend/runs/${id}/events?after_seq=${seq.current}`,
          { signal: ctrl.signal },
        );
        if (!response.ok || !response.body) throw new Error();
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        const consume = (chunk: string) => {
          const text = chunk
            .split("\n")
            .filter((line) => line.startsWith("data:"))
            .map((line) => line.slice(5).trimStart())
            .join("\n");
          if (!text) return;
          const event = parseEvent(JSON.parse(text));
          if (
            ctrl.signal.aborted ||
            activeRun.current !== id ||
            event.seq <= seq.current
          )
            return;
          seq.current = event.seq;
          try {
            sessionStorage.setItem(`cela:lastSeq:${id}`, String(event.seq));
          } catch {
            /* cursor remains in memory */
          }
          setEvents((prev) => [...prev.slice(-399), event]);
          if (
            [
              "approval_requested",
              "approval_resolved",
              "plan_created",
              "run_finished",
            ].includes(event.type)
          )
            void refreshSnapshot(id);
        };
        for (;;) {
          const { done, value } = await reader.read();
          if (ctrl.signal.aborted || activeRun.current !== id) {
            await reader.cancel();
            return;
          }
          buffer += decoder.decode(value, { stream: !done });
          const chunks = buffer.split(/\r?\n\r?\n/);
          buffer = chunks.pop() ?? "";
          for (const chunk of chunks) consume(chunk);
          if (done) {
            if (buffer.trim()) consume(buffer);
            break;
          }
          if (buffer.length > 2_000_000) throw new Error();
        }
        if (!ctrl.signal.aborted && activeRun.current === id) {
          setConnection("closed");
          void refreshSnapshot(id);
        }
      } catch {
        if (!ctrl.signal.aborted && activeRun.current === id && alive.current) {
          setConnection("error");
          setNotice(
            "انقطع بث النشاط أو كانت بياناته غير صالحة. حالة المهمة مستقلة؛ يمكنك إعادة الاتصال.",
          );
        }
      }
    },
    [refreshSnapshot],
  );
  const start = useCallback(
    async (task: string, mode: string) => {
      if (startingRef.current || !task.trim()) return;
      startingRef.current = true;
      setStarting(true);
      abort.current?.abort();
      activeRun.current = null;
      seq.current = 0;
      setRunId(null);
      snapshotRef.current = null;
      setSnapshot(null);
      setEvents([]);
      setConnection("idle");
      setNotice("");
      try {
        const response = await fetch("/api/backend/runs", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ task, mode, max_steps: 25 }),
        });
        if (!response.ok) throw new Error();
        const data: unknown = await response.json();
        if (
          !object(data) ||
          typeof data.run_id !== "string" ||
          !/^run_[a-zA-Z0-9_-]+$/.test(data.run_id)
        )
          throw new Error();
        if (!alive.current) return;
        activeRun.current = data.run_id;
        setRunId(data.run_id);
        try {
          const url = new URL(window.location.href);
          url.searchParams.set("run_id", data.run_id);
          window.history.replaceState(null, "", url.toString());
        } catch {
          /* history update optional */
        }
        setBackend("available");
        void refreshSnapshot(data.run_id);
        void stream(data.run_id);
      } catch {
        if (alive.current)
          setNotice(
            "تعذر إنشاء المهمة أو أن استجابة الخادم غير صالحة. لم نؤكد بدء التنفيذ؛ تحقق من الاتصال قبل المحاولة مجددًا.",
          );
      } finally {
        startingRef.current = false;
        if (alive.current) setStarting(false);
      }
    },
    [refreshSnapshot, stream],
  );
  const loadRun = useCallback(
    (id: string) => {
      if (!/^run_[a-zA-Z0-9_-]+$/.test(id)) return;
      abort.current?.abort();
      activeRun.current = id;
      seq.current = 0;
      setRunId(id);
      snapshotRef.current = null;
      setSnapshot(null);
      setEvents([]);
      setNotice("");
      try {
        const url = new URL(window.location.href);
        url.searchParams.set("run_id", id);
        window.history.replaceState(null, "", url.toString());
      } catch {
        /* history update optional */
      }
      void refreshSnapshot(id);
      void stream(id);
    },
    [refreshSnapshot, stream],
  );
  useEffect(() => {
    try {
      const param = new URLSearchParams(window.location.search).get("run_id");
      if (param && /^run_[a-zA-Z0-9_-]+$/.test(param) && !activeRun.current) {
        loadRun(param);
      }
    } catch {
      /* URLSearchParams optional */
    }
  }, [loadRun]);
  const approve = useCallback(
    async (id: string, decision: "approved" | "denied") => {
      const current = activeRun.current;
      if (!current || approvingRef.current) return;
      approvingRef.current = true;
      setApproving(id);
      setNotice("");
      try {
        const response = await fetch(`/api/backend/runs/${current}/approve`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ tool_call_id: id, decision }),
        });
        const data: unknown = await response.json();
        if (!response.ok || !object(data) || data.delivered !== true)
          throw new Error();
        void refreshSnapshot(current);
      } catch {
        if (alive.current && activeRun.current === current)
          setNotice(
            "لم تُؤكَّد استلام الموافقة. لا نفترض أن الإجراء نُفّذ؛ حدّث حالة المهمة.",
          );
      } finally {
        approvingRef.current = false;
        if (alive.current) setApproving(null);
      }
    },
    [refreshSnapshot],
  );
  const reset = useCallback(() => {
    abort.current?.abort();
    activeRun.current = null;
    seq.current = 0;
    setRunId(null);
    snapshotRef.current = null;
    setSnapshot(null);
    setEvents([]);
    setNotice("");
    setConnection("idle");
    try {
      const url = new URL(window.location.href);
      url.searchParams.delete("run_id");
      window.history.replaceState(null, "", url.toString());
    } catch {
      /* history update optional */
    }
  }, []);
  useEffect(() => {
    if (!runId) return;
    // Keep one stable clock per run, regardless of snapshots or SSE updates.
    const interval = setInterval(() => {
      const current = snapshotRef.current;
      if (current?.run_id === runId && TERMINAL.has(current.status)) return;
      void refreshSnapshot(runId);
    }, 1500);
    return () => clearInterval(interval);
  }, [runId, refreshSnapshot]);
  return {
    runId,
    events,
    snapshot,
    connection,
    backend,
    notice,
    starting,
    approving,
    execution: runId ? parseExecution(snapshot?.execution) : runtimeExecution,
    start,
    loadRun,
    approve,
    reset,
    reconnect: () => {
      if (runId) {
        setNotice("");
        void stream(runId);
      }
    },
  };
}
