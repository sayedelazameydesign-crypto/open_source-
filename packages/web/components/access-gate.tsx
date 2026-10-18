"use client";

import { useCallback, useEffect, useRef, useState, type CSSProperties, type FormEvent } from "react";

type GateState = "checking" | "open" | "locked" | "unconfigured" | "error";

interface AccessStatusResponse {
  required?: unknown;
  authorized?: unknown;
  configured?: unknown;
  mode?: unknown;
}

const overlayStyle: CSSProperties = {
  position: "fixed",
  inset: 0,
  zIndex: 1000,
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  background: "rgba(9, 12, 20, 0.72)",
  backdropFilter: "blur(6px)",
};

const cardStyle: CSSProperties = {
  width: "min(92vw, 380px)",
  padding: "28px",
  borderRadius: "16px",
  background: "#11182a",
  border: "1px solid rgba(255, 255, 255, 0.12)",
  color: "#eef2ff",
  display: "flex",
  flexDirection: "column",
  gap: "14px",
};

const inputStyle: CSSProperties = {
  padding: "12px 14px",
  borderRadius: "10px",
  border: "1px solid rgba(255, 255, 255, 0.18)",
  background: "rgba(255, 255, 255, 0.06)",
  color: "inherit",
  fontSize: "1rem",
};

const buttonStyle: CSSProperties = {
  padding: "12px 14px",
  borderRadius: "10px",
  border: "none",
  background: "#4f7cff",
  color: "#fff",
  fontSize: "1rem",
  cursor: "pointer",
};

function stateFromStatus(data: AccessStatusResponse): GateState {
  if (data.mode === "unconfigured" || (data.configured === false && data.required === true)) {
    return "unconfigured";
  }
  if (data.required === true && data.authorized === false) return "locked";
  if (
    (data.required === false && data.authorized === true) ||
    (data.required === true && data.authorized === true)
  ) {
    return "open";
  }
  return "error";
}

/**
 * Operator unlock / configuration-state overlay for the optional access gate
 * (`CELA_WEB_ACCESS_SECRET`, see `lib/access-gate.ts`).
 *
 * The UI mirrors the proxies' security state instead of guessing:
 *
 * * locked → ask for the operator secret;
 * * unconfigured → say the server gate is missing its secret (the proxies are
 *   refusing with `503 access_gate_unconfigured`); no password prompt can fix it;
 * * error → the status check itself failed; show a retry, never fall through
 *   to an "open" workspace just because `/api/access` could not be read.
 *
 * A successful unlock posts the secret to `/api/access`, which sets an
 * `HttpOnly` signed-proof cookie. The secret is never stored in page JS state
 * beyond the form field or in `localStorage`.
 */
export function AccessGate() {
  const [state, setState] = useState<GateState>("checking");
  const [secret, setSecret] = useState("");
  const [failed, setFailed] = useState(false);
  const [busy, setBusy] = useState(false);
  const checkSequence = useRef(0);

  const checkAccess = useCallback(async () => {
    const sequence = ++checkSequence.current;
    setState("checking");
    setFailed(false);
    try {
      const res = await fetch("/api/access", { cache: "no-store" });
      if (!res.ok) throw new Error(`access status failed with ${res.status}`);
      const data = (await res.json()) as AccessStatusResponse;
      if (sequence === checkSequence.current) setState(stateFromStatus(data));
    } catch {
      if (sequence === checkSequence.current) setState("error");
    }
  }, []);

  useEffect(() => {
    void checkAccess();
    return () => {
      checkSequence.current += 1;
    };
  }, [checkAccess]);

  const unlock = useCallback(
    async (event: FormEvent) => {
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
        } else if (res.status === 401) {
          setFailed(true);
        } else if (res.status === 409) {
          setState("unconfigured");
        } else {
          setState("error");
        }
      } catch {
        setState("error");
      } finally {
        setBusy(false);
      }
    },
    [secret],
  );

  if (state === "checking" || state === "open") return null;

  if (state === "locked") {
    return (
      <div role="dialog" aria-modal="true" aria-label="الوصول محمي" style={overlayStyle}>
        <form onSubmit={unlock} style={cardStyle}>
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
            style={inputStyle}
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
              ...buttonStyle,
              cursor: busy ? "wait" : buttonStyle.cursor,
              opacity: busy || secret.length === 0 ? 0.6 : 1,
            }}
          >
            {busy ? "جارٍ التحقق…" : "فتح"}
          </button>
        </form>
      </div>
    );
  }

  const unconfigured = state === "unconfigured";
  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label={unconfigured ? "إعداد الوصول غير مكتمل" : "تعذّر التحقق من حالة الوصول"}
      style={overlayStyle}
    >
      <section style={cardStyle} role="alert">
        <h2 style={{ margin: 0, fontSize: "1.15rem" }}>
          {unconfigured ? "إعداد الوصول غير مكتمل" : "تعذّر التحقق من حالة الوصول"}
        </h2>
        <p style={{ margin: 0, opacity: 0.85, lineHeight: 1.7 }}>
          {unconfigured ? (
            <>
              لم يُهيّأ الخادم بسرّ وصول المشغّل (<span dir="ltr">CELA_WEB_ACCESS_SECRET</span>)،
              لذلك يرفض وكيل الخلفية الطلبات بدل فتحها. أكمل إعداد الخادم ثم أعد المحاولة.
            </>
          ) : (
            <>
              لم تصل الواجهة إلى نقطة التحقق <span dir="ltr">/api/access</span> أو لم تستطع قراءة
              ردّها. لن تُفتح المساحة حتى ينجح التحقق من حالة الوصول.
            </>
          )}
        </p>
        <button type="button" onClick={() => void checkAccess()} style={buttonStyle}>
          إعادة المحاولة
        </button>
      </section>
    </div>
  );
}
