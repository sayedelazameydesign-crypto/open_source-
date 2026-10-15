"use client";

import { useRef, useState } from "react";
import { Icon } from "../../components/icon";
import {
  EmptyState,
  LoadingState,
  Notice,
  StatusPill,
} from "../../components/workspace/ui";
import { object } from "../../lib/contracts";

type DocumentRun = {
  run_id: string;
  status: string;
  plan: string[];
  error: string | null;
  statistics: Record<string, number> | null;
  artifact: { artifact_id: string; sha256: string; size_bytes: number } | null;
};

const labels: Record<string, string> = {
  RECEIVED: "تم الاستلام",
  PLAN_READY: "الخطة جاهزة",
  AWAITING_CONFIRMATION: "بانتظار موافقتك",
  RUNNING: "قيد التنفيذ",
  SUCCEEDED: "اكتملت المعالجة",
  FAILED: "فشلت المعالجة",
};

export default function Documents() {
  const [file, setFile] = useState<File | null>(null);
  const [run, setRun] = useState<DocumentRun | null>(null);
  const [restoreId, setRestoreId] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const pending = useRef(false);

  async function request(path: string, options?: RequestInit) {
    if (pending.current) return;
    pending.current = true;
    setBusy(true);
    setError("");
    try {
      const response = await fetch(
        `/api/backend/document-runs${path}`,
        options,
      );
      if (!response.ok) throw new Error("request_failed");
      const result: unknown = await response.json();
      if (
        !object(result) ||
        typeof result.run_id !== "string" ||
        !/^doc_[0-9a-f]{32}$/.test(result.run_id) ||
        typeof result.status !== "string" ||
        !Object.hasOwn(labels, result.status) ||
        !Array.isArray(result.plan) ||
        !result.plan.every((v) => typeof v === "string") ||
        (result.error !== null && typeof result.error !== "string") ||
        (result.statistics !== null &&
          (!object(result.statistics) ||
            !Object.values(result.statistics).every(
              (v) => typeof v === "number",
            ))) ||
        (result.artifact !== null &&
          (!object(result.artifact) ||
            typeof result.artifact.artifact_id !== "string" ||
            !/^report_[0-9a-f]{32}\.md$/.test(result.artifact.artifact_id) ||
            typeof result.artifact.sha256 !== "string" ||
            typeof result.artifact.size_bytes !== "number"))
      ) {
        throw new Error("invalid_response");
      }
      setRun(result as DocumentRun);
      setRestoreId(result.run_id);
    } catch {
      setError(
        "تعذر إتمام الطلب أو أن استجابة الخادم غير صالحة. لم نؤكد نجاح المعالجة؛ تحقق من الملف والاتصال.",
      );
    } finally {
      pending.current = false;
      setBusy(false);
    }
  }

  return (
    <main className="documents-page">
      <span className="eyebrow">
        ملف واحد. معالجة محلية. نتيجة قابلة للمراجعة.
      </span>
      <h1>المستندات النصية</h1>
      <div className="documents-intro">
        <p role="note">
          وضع التشغيل: معالجة محلية حتمية — بلا نموذج ذكاء اصطناعي.
        </p>
        <p>
          ملف TXT واحد بترميز UTF-8، حتى 1 MiB. إحصاءات ومقتطف فقط، وليست
          تلخيصًا ذكيًا.
        </p>
        <p>
          لن تبدأ المعالجة قبل عرض الخطة وتأكيدك. يُحفظ النص مؤقتًا في سجل
          المهمة، ويحتوي التقرير على مقتطف منه. لا ترفع أسرارًا.
        </p>
      </div>
      <div className="documents-grid">
        <div>
          <section className="panel document-upload">
            <div className="panel-heading">
              <h2>ابدأ بملف نصي</h2>
              <Icon name="file" />
            </div>
            <label htmlFor="document">اختر ملفًا نصيًا</label>{" "}
            <input
              id="document"
              type="file"
              accept=".txt"
              disabled={busy}
              onChange={(e) => {
                setFile(e.target.files?.[0] ?? null);
                setError("");
              }}
            />
            <button
              className="primary-button"
              disabled={!file || busy}
              onClick={() => {
                if (!file) return;
                if (file.size > 1048576) {
                  setError("حجم الملف يتجاوز 1 MiB");
                  return;
                }
                setRun(null);
                request(`?filename=${encodeURIComponent(file.name)}`, {
                  method: "POST",
                  body: file,
                  headers: { "Content-Type": "application/octet-stream" },
                });
              }}
            >
              رفع الملف وعرض الخطة فقط
            </button>
          </section>
          {busy && <LoadingState>جارٍ إرسال الطلب…</LoadingState>}
          {error && <Notice>{error}</Notice>}
          {run && (
            <section className="panel document-result" aria-live="polite">
              <div className="panel-heading">
                <h2>{labels[run.status]}</h2>
                <StatusPill
                  status={
                    run.status === "FAILED"
                      ? "failed"
                      : run.status === "RUNNING"
                        ? "running"
                        : "pending"
                  }
                >
                  {labels[run.status]}
                </StatusPill>
              </div>
              <p className="document-run-id">
                معرّف المهمة:{" "}
                <code style={{ overflowWrap: "anywhere" }}>{run.run_id}</code>
              </p>
              <ol>
                {run.plan.map((step, i) => (
                  <li key={i}>{step}</li>
                ))}
              </ol>
              {run.status === "AWAITING_CONFIRMATION" && (
                <button
                  className="primary-button"
                  disabled={busy}
                  onClick={() =>
                    request(`/${run.run_id}/execute`, { method: "POST" })
                  }
                >
                  أوافق على تنفيذ الخطة محليًا
                </button>
              )}
              {run.error && (
                <p role="alert">فشلت المعالجة. لم يُعلن نجاح المهمة.</p>
              )}
              {run.statistics && (
                <pre dir="ltr">{JSON.stringify(run.statistics, null, 2)}</pre>
              )}
              {run.artifact && (
                <>
                  <a
                    download
                    href={`/api/backend/document-runs/${run.run_id}/artifacts/${run.artifact.artifact_id}/download`}
                  >
                    تنزيل تقرير Markdown
                  </a>
                  <p className="checksum">
                    SHA-256: <code>{run.artifact.sha256}</code>
                  </p>
                </>
              )}
            </section>
          )}
          {!run && (
            <section className="panel">
              <EmptyState icon="file" title="لم تبدأ معالجة بعد">
                ارفع ملف TXT لعرض خطة حقيقية قبل الموافقة على تنفيذها.
              </EmptyState>
            </section>
          )}
        </div>
        <aside>
          <section className="panel restore-panel">
            <span className="eyebrow">متابعة عمل سابق</span>
            <h2>استرجاع مستند</h2>
            <label htmlFor="restore">استرجاع مهمة محفوظة بمعرّفها</label>{" "}
            <input
              id="restore"
              value={restoreId}
              dir="ltr"
              style={{ maxWidth: "100%" }}
              onChange={(e) => setRestoreId(e.target.value)}
            />
            <button
              className="secondary-button"
              disabled={busy || !/^doc_[0-9a-f]{32}$/.test(restoreId)}
              onClick={() => request(`/${restoreId}`)}
            >
              استرجاع السجل
            </button>
            <p className="section-note">
              الاسترجاع بمعرّف معروف فقط. لا تتوفر قائمة شاملة للمهام هنا.
            </p>
          </section>
        </aside>
      </div>
    </main>
  );
}
