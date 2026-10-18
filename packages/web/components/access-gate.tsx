"use client";

import { useCallback, useEffect, useState } from "react";

/**
 * Operator unlock overlay for the optional access gate
 * (`CELA_WEB_ACCESS_SECRET`, see `lib/access-gate.ts`).
 *
 * Shown only when the gate is required and this browser has not unlocked yet.
 * The secret goes to `POST /api/access`, which sets an `HttpOnly` cookie the
 * `/api/backend/*` proxies accept — it is never stored in page JS or
 * `localStorage`, and all existing same-origin `fetch` calls pick the cookie
 * up automatically.
 */
export function AccessGate() {
  const [state, setState] = useState<"checking" | "open" | "locked">("checking");
  const [secret, setSecret] = useState("");
  const [failed, setFailed] = useState(false);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let cancelled = false;
    fetch("/api/access")
      .then((res) => (res.ok ? res.json() : null))
      .then((data: { required?: boolean; authorized?: boolean } | null) => {
        if (cancelled || !data) return;
        setState(data.required && !data.authorized ? "locked" : "open");
      })
      .catch(() => {
        if (!cancelled) setState("open");
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const unlock = useCallback(
    async (event: React.FormEvent) => {
      event.preventDefault();
      setBusy(true);
      setFailed(false);
      try {
        const res = await fetch("/api/access", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ secret }),
        });
        if (res.ok) {
          setSecret("");
          setState("open");
        } else {
          setFailed(true);
        }
      } catch {
        setFailed(true);
      } finally {
        setBusy(false);
      }
    },
    [secret],
  );

  if (state !== "locked") return null;

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label="الوصول محمي"
      style={{
        position: "fixed",
        inset: 0,
        zIndex: 1000,
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        background: "rgba(9, 12, 20, 0.72)",
        backdropFilter: "blur(6px)",
      }}
    >
      <form
        onSubmit={unlock}
        style={{
          width: "min(92vw, 380px)",
          padding: "28px",
          borderRadius: "16px",
          background: "#11182a",
          border: "1px solid rgba(255, 255, 255, 0.12)",
          color: "#eef2ff",
          display: "flex",
          flexDirection: "column",
          gap: "14px",
        }}
      >
        <h2 style={{ margin: 0, fontSize: "1.15rem" }}>الوصول محمي</h2>
        <p style={{ margin: 0, opacity: 0.8, lineHeight: 1.7 }}>
          هذه المساحة تعمل برمز وصول للمشغّل. أدخل الرمز للمتابعة.
        </p>
        <input
          type="password"
          value={secret}
          onChange={(event) => setSecret(event.target.value)}
          placeholder="رمز الوصول"
          aria-label="رمز الوصول"
          autoFocus
          style={{
            padding: "12px 14px",
            borderRadius: "10px",
            border: "1px solid rgba(255, 255, 255, 0.18)",
            background: "rgba(255, 255, 255, 0.06)",
            color: "inherit",
            fontSize: "1rem",
          }}
        />
        {failed ? (
          <p style={{ margin: 0, color: "#fca5a5" }} role="alert">
            رمز غير صحيح — حاول مرة أخرى.
          </p>
        ) : null}
        <button
          type="submit"
          disabled={busy || secret.length === 0}
          style={{
            padding: "12px 14px",
            borderRadius: "10px",
            border: "none",
            background: "#4f7cff",
            color: "#fff",
            fontSize: "1rem",
            cursor: busy ? "wait" : "pointer",
            opacity: busy || secret.length === 0 ? 0.6 : 1,
          }}
        >
          {busy ? "جارٍ التحقق…" : "فتح"}
        </button>
      </form>
    </div>
  );
}
