"use client";

import { useState, type ReactNode } from "react";

import { Icon } from "../icon";

/**
 * Small, shared presentation pieces for the four read-only console pages.
 *
 * These components never fetch and never decide platform state: they render
 * what a page has already validated. A missing value is shown as an explicit
 * "not configured" fact instead of a fabricated placeholder.
 */

export function ConsoleHeading({
  eyebrow,
  title,
  lede,
  children,
}: {
  eyebrow: string;
  title: string;
  lede: ReactNode;
  children?: ReactNode;
}) {
  return (
    <div className="workspace-heading console-heading">
      <div>
        <span className="eyebrow">{eyebrow}</span>
        <h1>{title}</h1>
        <p className="console-lede">{lede}</p>
      </div>
      {children ? <div className="heading-actions">{children}</div> : null}
    </div>
  );
}

export function RefreshButton({
  onClick,
  loading,
}: {
  onClick: () => void;
  loading: boolean;
}) {
  return (
    <button type="button" className="secondary-button" onClick={onClick} disabled={loading}>
      <Icon name="refresh" width="15" />
      {loading ? "جارٍ التحديث…" : "تحديث البيانات"}
    </button>
  );
}

export function ConsolePanel({
  eyebrow,
  title,
  status,
  actions,
  children,
}: {
  eyebrow: string;
  title: string;
  status?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section className="panel console-panel">
      <div className="panel-heading">
        <div>
          <span className="eyebrow">{eyebrow}</span>
          <h2>{title}</h2>
        </div>
        {status ? <div className="console-panel-status">{status}</div> : null}
        {actions ? <div className="console-panel-actions">{actions}</div> : null}
      </div>
      {children}
    </section>
  );
}

export function Fact({
  label,
  value,
  mono = false,
  ltr = false,
  note,
}: {
  label: string;
  value: ReactNode;
  mono?: boolean;
  ltr?: boolean;
  note?: ReactNode;
}) {
  return (
    <div className="fact">
      <span className="fact-label">{label}</span>
      <span
        className={`fact-value${mono ? " mono" : ""}`}
        dir={ltr ? "ltr" : undefined}
      >
        {value}
      </span>
      {note ? <span className="fact-note">{note}</span> : null}
    </div>
  );
}

export function FactGrid({ children }: { children: ReactNode }) {
  return <div className="fact-grid">{children}</div>;
}

/** An explicit absence, never a blank cell that reads as "unknown but fine". */
export function NotConfigured({ children }: { children: ReactNode }) {
  return <span className="fact-missing">{children}</span>;
}

export function CopyButton({ value, label }: { value: string; label: string }) {
  const [state, setState] = useState<"idle" | "copied" | "failed">("idle");
  const disabled = value.length === 0;
  return (
    <span className="copy-wrap">
      <button
        type="button"
        className="secondary-button copy-button"
        disabled={disabled}
        onClick={() => {
          void (async () => {
            try {
              if (!navigator.clipboard) throw new Error("clipboard_unavailable");
              await navigator.clipboard.writeText(value);
              setState("copied");
            } catch {
              setState("failed");
            }
          })();
        }}
      >
        <Icon name="file" width="14" />
        {label}
      </button>
      <span className="copy-status" role="status" aria-live="polite">
        {state === "copied"
          ? "نُسخ إلى الحافظة."
          : state === "failed"
            ? "تعذر النسخ — انسخ القيمة يدويًا."
            : ""}
      </span>
    </span>
  );
}

/** A footnote that names where a value came from, so it is auditable. */
export function DataOrigin({ children }: { children: ReactNode }) {
  return (
    <p className="console-origin">
      <Icon name="shield" width="14" />
      {children}
    </p>
  );
}
