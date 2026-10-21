"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { Icon } from "../../components/icon";
import { buildWorkspaceHref } from "../../lib/page-links";

type CatalogApp = {
  id: string;
  name: string;
  tagline: string;
  group: string;
  repo: string;
  releases_url: string;
  former_ids: string[];
};

type UpdateRow = {
  app_id: string;
  name: string;
  installed: string;
  latest: string | null;
  update_available: boolean;
  detail: string;
};

// Launcher panel (ADR-0015): a read-only window onto the Craft App Suite.
// Everything goes through the server-side proxy (/api/backend/*), so no token
// ever reaches the browser. The surface is opt-in on the backend
// (CELA_LAUNCHER_ENABLE=1) and answers 503 otherwise — that is shown, not hidden.
export default function LauncherPage() {
  const [apps, setApps] = useState<CatalogApp[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [installedText, setInstalledText] = useState('{\n  "photocraft": "0.5.0"\n}');
  const [updates, setUpdates] = useState<UpdateRow[] | null>(null);
  const [checking, setChecking] = useState(false);
  const [checkNote, setCheckNote] = useState<string | null>(null);

  const loadCatalog = useCallback(async () => {
    setError(null);
    try {
      const res = await fetch("/api/backend/api/launcher/catalog");
      const body = await res.json();
      if (!res.ok) {
        setError(
          (body && (body.detail as string)) || `catalog request failed (${res.status})`,
        );
        return;
      }
      setApps(body.apps as CatalogApp[]);
    } catch (err) {
      setError(String(err));
    }
  }, []);

  useEffect(() => {
    void loadCatalog();
  }, [loadCatalog]);

  async function checkUpdates() {
    setChecking(true);
    setCheckNote(null);
    setUpdates(null);
    try {
      const installed = JSON.parse(installedText) as Record<string, string>;
      const res = await fetch("/api/backend/api/launcher/check", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ installed }),
      });
      const body = await res.json();
      if (!res.ok) {
        setCheckNote((body && (body.detail as string)) || `check failed (${res.status})`);
        return;
      }
      setUpdates(body.apps as UpdateRow[]);
    } catch (err) {
      setCheckNote(String(err));
    } finally {
      setChecking(false);
    }
  }

  return (
    <div className="shell">
      <header style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
        <h1>
          Craft App Suite <span>لوحة اللانشر — قراءة فقط</span>
        </h1>
        <div className="heading-actions">
          <Link className="secondary-button" href="/launchpad">
            <Icon name="grid" width="15" /> لوحة التشغيل
          </Link>
          <Link
            className="secondary-button"
            href={buildWorkspaceHref({
              task: "افحص كتالوج تطبيقات Craft App Suite وحالة التحديثات المتاحة",
              mode: "flow",
            })}
          >
            <Icon name="arrow" width="15" /> فتح في مساحة العمل
          </Link>
        </div>
      </header>
      <p className="conn">
        الكتالوج والإصدارات عبر <code>/api/launcher/*</code> — لا تنزيل ولا تثبيت من هنا.
      </p>

      {error && <div className="notice">{error}</div>}

      {apps && (
        <div className="pane" style={{ marginBottom: 14 }}>
          <h2>الكتالوج ({apps.length} تطبيقًا)</h2>
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
            <thead>
              <tr style={{ color: "var(--mut)", textAlign: "left" }}>
                <th style={{ padding: "4px 8px" }}>التطبيق</th>
                <th style={{ padding: "4px 8px" }}>الوصف</th>
                <th style={{ padding: "4px 8px" }}>الجيل</th>
                <th style={{ padding: "4px 8px" }}>المستودع</th>
              </tr>
            </thead>
            <tbody>
              {apps.map((app) => (
                <tr key={app.id} style={{ borderTop: "1px dashed var(--line)" }}>
                  <td style={{ padding: "6px 8px" }}>{app.name}</td>
                  <td style={{ padding: "6px 8px", color: "var(--mut)" }}>{app.tagline}</td>
                  <td style={{ padding: "6px 8px" }}>{app.group}</td>
                  <td style={{ padding: "6px 8px" }}>
                    <a href={app.releases_url} style={{ color: "var(--acc)" }}>
                      {app.repo}
                    </a>
                    {app.former_ids.length > 0 && (
                      <code style={{ color: "var(--mut)", marginRight: 6 }}>
                        {" "}
                        (سابقًا: {app.former_ids.join(", ")})
                      </code>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <div className="pane">
        <h2>فحص التحديثات</h2>
        <p className="empty" style={{ marginTop: 0 }}>
          الصق كائن JSON من معرّف التطبيق إلى النسخة المثبَّتة لديك:
        </p>
        <div className="composer">
          <textarea
            value={installedText}
            onChange={(e) => setInstalledText(e.target.value)}
            rows={5}
            style={{
              flex: 1,
              background: "var(--bg)",
              color: "var(--txt)",
              border: "1px solid var(--line)",
              borderRadius: 8,
              padding: "9px 12px",
              fontFamily: "ui-monospace, monospace",
              fontSize: 13,
            }}
          />
        </div>
        <button onClick={() => void checkUpdates()} disabled={checking}>
          {checking ? "جارٍ الفحص…" : "فحص التحديثات"}
        </button>
        {checkNote && <div className="notice" style={{ marginTop: 10 }}>{checkNote}</div>}
        {updates && (
          <ul className="steps" style={{ marginTop: 12 }}>
            {updates.map((row) => (
              <li key={row.app_id}>
                <strong>{row.name}</strong>{" "}
                <code>
                  {row.installed} → {row.latest ?? "—"}
                </code>{" "}
                {row.update_available ? (
                  <span style={{ color: "var(--warn)" }}>تحديث متاح</span>
                ) : row.detail ? (
                  <span className="empty">({row.detail})</span>
                ) : (
                  <span style={{ color: "var(--acc)" }}>محدَّث</span>
                )}
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
