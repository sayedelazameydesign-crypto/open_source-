import { Icon } from "../icon";
import { EmptyState, StatusPill } from "./ui";
import {
  computeMissionStages,
  type AgentEvent,
  type PlanStep,
  type Snapshot,
} from "../../lib/contracts";

const eventLabels: Record<string, string> = {
  run_started: "بدأت دورة المهمة",
  step_started: "بدأت خطوة تنفيذ",
  step_finished: "انتهت خطوة تنفيذ",
  model_selected: "اقتُرحت وجهة توجيه",
  thought: "رسالة من المنفذ — ليست دليل تحقق",
  plan_created: "أُنشئت خطة التنفيذ",
  plan_updated: "حُدثت الخطة",
  tool_call: "طلب استدعاء أداة",
  tool_result: "سُجلت نتيجة أداة",
  artifact: "سُجل مُخرج",
  run_finished: "انتهت دورة المحرك",
  error: "سُجل خطأ في المهمة",
  warning: "تنبيه تشغيلي",
  log: "حدث تشغيلي",
  approval_requested: "طُلبت موافقة بشرية",
  approval_resolved: "عولج طلب الموافقة",
  browser_action: "سُجل إجراء متصفح — قد يكون تجريبيًا",
  review: "سُجلت مراجعة للمحرك",
};
export function StepCard({ step, index }: { step: PlanStep; index: number }) {
  const title = step.title.replace(/^react step (\d+)$/, "خطوة التنفيذ $1");
  return (
    <li className="step-card">
      <span className={`step-number ${step.status}`}>
        {step.status === "done" ? <Icon name="check" width="16" /> : index + 1}
      </span>
      <div>
        <h3>{title}</h3>
        {step.tool && <code className="technical">{step.tool}</code>}
      </div>
      <StatusPill status={step.status} />
    </li>
  );
}
export function PlanPanel({ steps }: { steps: PlanStep[] }) {
  return (
    <section className="panel plan-panel" aria-label="خطة المهمة">
      <div className="panel-heading">
        <div>
          <span className="eyebrow">خطوة بخطوة</span>
          <h2>خطة المهمة</h2>
        </div>
        <span className="count-label">{steps.length} خطوات</span>
      </div>
      {steps.length ? (
        <ol className="step-list">
          {steps.map((step, i) => (
            <StepCard key={step.id} step={step} index={i} />
          ))}
        </ol>
      ) : (
        <EmptyState compact icon="layers" title="لم تصل خطة بعد">
          ستظهر الخطوات التي يرسلها الخادم هنا، دون تخمين خطة من نص الإجابة.
        </EmptyState>
      )}
    </section>
  );
}
export function ActivityPanel({ events }: { events: AgentEvent[] }) {
  return (
    <section className="panel" aria-label="نشاط المهمة">
      <div className="panel-heading">
        <div>
          <span className="eyebrow">ما حدث فعلًا</span>
          <h2>نشاط المهمة</h2>
        </div>
        <Icon name="activity" />
      </div>
      <p className="section-note">
        ملخصات أحداث الخادم؛ لا نعرض استدلالًا داخليًا مفترضًا أو وسائط الأدوات
        الحساسة.
      </p>
      {events.length ? (
        <ol className="activity-list">
          {events.map((event) => (
            <li key={event.seq}>
              <span className="event-node">
                <Icon
                  name={
                    event.type === "error"
                      ? "alert"
                      : event.type === "tool_result"
                        ? event.data.status === "success"
                          ? "check"
                          : "alert"
                        : "activity"
                  }
                  width="15"
                />
              </span>
              <div>
                <strong>
                  {Object.hasOwn(eventLabels, event.type)
                    ? eventLabels[event.type]
                    : "حدث من الخادم"}
                </strong>
                {typeof event.data.name === "string" && (
                  <code className="technical">{event.data.name}</code>
                )}
                {event.type === "tool_result" &&
                  typeof event.data.status === "string" && (
                    <StatusPill status={event.data.status} />
                  )}
                {event.type === "approval_resolved" &&
                  event.data.decision === "denied" && (
                    <StatusPill status="denied" />
                  )}
                {event.type === "approval_resolved" &&
                  event.data.decision === "approved" && (
                    <StatusPill status="pending">
                      سُجلت الموافقة — ليست دليل تنفيذ
                    </StatusPill>
                  )}
                <details className="event-details">
                  <summary>مرجع الحدث</summary>
                  <code dir="ltr">
                    {event.type} · #{event.seq}
                  </code>
                </details>
              </div>
              <span className="event-seq" aria-label={`الحدث ${event.seq}`}>
                {String(event.seq).padStart(2, "0")}
              </span>
            </li>
          ))}
        </ol>
      ) : (
        <EmptyState compact icon="activity" title="لا نشاط مسجل بعد">
          ستصل التحديثات هنا عند بدء المهمة واتصال البث.
        </EmptyState>
      )}
    </section>
  );
}
export function MissionStagesBar({
  snapshot,
  steps = [],
}: {
  snapshot: Snapshot | null;
  steps?: PlanStep[];
}) {
  const stages = computeMissionStages(snapshot, steps);
  const stageStatusLabel: Record<string, string> = {
    idle: "لم تبدأ",
    active: "جارية",
    done: "مكتملة",
    failed: "توقفت",
  };
  return (
    <section
      className="panel mission-stages-bar"
      aria-label="مراحل مهمة Celia.sh — Mission OS"
    >
      <div className="panel-heading">
        <div>
          <span className="eyebrow">Celia.sh — Mission OS</span>
          <h2>مسار المهمة من الفهم إلى التقرير الموثّق</h2>
        </div>
        <span className="count-label">4 مراحل</span>
      </div>
      <ol className="mission-stage-list">
        {stages.map((stage, idx) => (
          <li
            key={stage.id}
            className={`mission-stage-item stage-${stage.status}`}
          >
            <span className="mission-stage-index">
              {stage.status === "done" ? (
                <Icon name="check" width="15" />
              ) : (
                idx + 1
              )}
            </span>
            <div>
              <strong>{stage.titleAr}</strong>
              <small dir="ltr">{stage.titleEn}</small>
            </div>
            <span className="mission-stage-badge">
              {stageStatusLabel[stage.status]}
            </span>
          </li>
        ))}
      </ol>
    </section>
  );
}
export function EvidencePanel({
  snapshot,
  runId,
}: {
  snapshot: Snapshot | null;
  runId: string | null;
}) {
  const artifacts = snapshot?.artifacts ?? [];
  return (
    <aside className="evidence-column" aria-label="الأدلة والملفات">
      <section className="panel evidence-files">
        <div className="panel-heading">
          <div>
            <span className="eyebrow">مساحة العمل</span>
            <h2>الأدلة والملفات</h2>
          </div>
          <Icon name="folder" />
        </div>
        <div className="evidence-subhead">
          <span>مخرجات المهمة</span>
          <span className="count-label">{artifacts.length}</span>
        </div>
        {artifacts.length ? (
          <ul className="artifact-list">
            {artifacts.map((artifact) => (
              <li key={artifact.filename}>
                <span className="file-icon">
                  <Icon name="file" />
                </span>
                <div>
                  <strong className="technical">{artifact.filename}</strong>
                  <small>{artifact.mime_type}</small>
                  {artifact.sha256 && (
                    <small dir="ltr" className="technical">
                      sha256:{artifact.sha256.slice(0, 12)}…
                    </small>
                  )}
                  {runId && (
                    <div className="artifact-links">
                      <a
                        className="text-link"
                        href={`/api/backend/runs/${encodeURIComponent(runId)}/artifacts/${encodeURIComponent(artifact.filename)}/download`}
                        download={artifact.filename}
                      >
                        تنزيل الملف الموثّق (SHA-256){" "}
                        <Icon name="arrow" width="13" />
                      </a>
                      <a
                        className="text-link"
                        href={`/api/backend/artifacts/${encodeURIComponent(runId)}/${encodeURIComponent(artifact.filename)}`}
                        target="_blank"
                        rel="noreferrer"
                      >
                        فتح بيانات الملف <Icon name="link" width="13" />
                      </a>
                    </div>
                  )}
                </div>
              </li>
            ))}
          </ul>
        ) : (
          <EmptyState compact icon="file" title="لم تُنشأ ملفات بعد">
            المخرجات التي ترسلها المهمة ستظهر هنا. لا نعرض ملفات تجريبية ثابتة.
          </EmptyState>
        )}
        <a className="document-shortcut" href="/documents">
          <Icon name="file" />
          <span>
            <strong>لديك ملف TXT؟</strong>
            <small>حلّله محليًا وأنشئ تقريرًا قابلًا للتنزيل</small>
          </span>
          <Icon name="arrow" width="17" />
        </a>
        {artifacts.length > 0 && (
          <p className="section-note">
            المسار العام يعيد بيانات الملف بصيغة JSON؛ ليس رابط تنزيل مباشر.
            تنزيل Markdown متاح في رحلة TXT، أو عبر رابط التنزيل الموثّق بـ
            SHA-256 أعلاه.
          </p>
        )}
      </section>
      <section className="panel verification-panel">
        <span className="verification-icon">
          <Icon name="shield" />
        </span>
        <h2>وضوح قبل الثقة</h2>
        <p>
          انتهاء المهمة أو نجاح أداة لا يثبت وحده تحقيق هدفك. راجع المخرجات
          ومصدرها.
        </p>
        <span className="verification-tag">الهدف غير متحقق استقلاليًا</span>
        <dl className="evidence-metrics">
          <div>
            <dt>نتائج أدوات مسجلة</dt>
            <dd>
              {snapshot?.tool_results_summary?.recorded ??
                snapshot?.recorded_tool_results ??
                "—"}
            </dd>
          </div>
          <div>
            <dt>نتائج غير ناجحة</dt>
            <dd>{snapshot?.tool_results_summary?.unsuccessful ?? "—"}</dd>
          </div>
        </dl>
      </section>
      <details className="panel routing-details">
        <summary>
          تفاصيل التوجيه المقترح <Icon name="layers" width="16" />
        </summary>
        <h2>توجيه مقترح — لا يثبت استدعاء النموذج</h2>
        {(snapshot?.models ?? []).length ? (
          <ul>
            {snapshot?.models?.map((model, i) => (
              <li key={i}>
                <code className="technical">{model.model}</code>
                <small>{model.role}</small>
              </li>
            ))}
          </ul>
        ) : (
          <p className="section-note">لم يصل توجيه لهذه المهمة بعد.</p>
        )}
      </details>
    </aside>
  );
}
