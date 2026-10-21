"use client";

import { useSearchParams } from "next/navigation";
import { Suspense, useEffect, useMemo, useRef, useState } from "react";
import { ExecutionDisclosure } from "../components/execution-disclosure";
import { Icon } from "../components/icon";
import {
  ActivityPanel,
  EvidencePanel,
  MissionStagesBar,
  PlanPanel,
} from "../components/workspace/panels";
import { LoadingState, Notice, StatusPill } from "../components/workspace/ui";
import { useAgentRun } from "../hooks/use-agent-run";
import { object, TERMINAL, type PlanStep } from "../lib/contracts";
import { parseWorkspaceParams, type WorkspaceMode } from "../lib/page-links";

function WorkspaceQuerySync({
  onParams,
}: {
  onParams: (params: {
    runId: string | null;
    task: string | null;
    mode: WorkspaceMode | null;
  }) => void;
}) {
  const searchParams = useSearchParams();
  const raw = searchParams.toString();
  const lastApplied = useRef<string | null>(null);
  useEffect(() => {
    if (!raw || lastApplied.current === raw) return;
    lastApplied.current = raw;
    onParams(parseWorkspaceParams(raw));
  }, [raw, onParams]);
  return null;
}

type Tab = "task" | "activity" | "evidence";
const tabs: { id: Tab; title: string }[] = [
  { id: "task", title: "المهمة" },
  { id: "activity", title: "النشاط" },
  { id: "evidence", title: "الأدلة والملفات" },
];

export default function Home() {
  const run = useAgentRun();
  const [task, setTask] = useState("");
  const [mode, setMode] = useState("auto");
  const [tab, setTab] = useState<Tab>("task");
  const prompt = useRef<HTMLTextAreaElement>(null);
  useEffect(() => {
    const adapt = () => {
      if (window.innerWidth > 1100 && tab === "evidence") setTab("task");
    };
    window.addEventListener("resize", adapt);
    return () => window.removeEventListener("resize", adapt);
  }, [tab]);
  const active = Boolean(
    run.runId && (!run.snapshot || !TERMINAL.has(run.snapshot.status)),
  );
  const steps = useMemo(() => {
    if (run.snapshot?.steps?.length) return run.snapshot.steps;
    const latest = [...run.events]
      .reverse()
      .find(
        (e) =>
          ["plan_created", "plan_updated"].includes(e.type) &&
          Array.isArray(e.data.steps),
      );
    return ((latest?.data.steps as unknown[] | undefined) ?? []).filter(
      (v): v is PlanStep =>
        object(v) &&
        typeof v.id === "string" &&
        typeof v.title === "string" &&
        typeof v.status === "string" &&
        (v.tool === null || typeof v.tool === "string"),
    );
  }, [run.snapshot, run.events]);
  const selectExample = (value: string, selectedMode: string) => {
    setTask(value);
    setMode(selectedMode);
    prompt.current?.focus();
  };
  const newTask = () => {
    run.reset();
    setTask("");
    setTab("task");
    prompt.current?.focus();
  };
  const connectionText = {
    idle: "البث غير مبدوء",
    live: "البث متصل",
    resuming: "استئناف البث",
    closed: "انتهى البث — حالة المهمة مستقلة",
    error: "انقطع البث — لا يعني فشل المهمة",
  }[run.connection];
  return (
    <main className="workspace-page">
      <Suspense fallback={null}>
        <WorkspaceQuerySync
          onParams={({
            runId: incomingRunId,
            task: incomingTask,
            mode: incomingMode,
          }) => {
            if (run.starting) return;
            if (incomingRunId && run.runId !== incomingRunId) {
              run.loadRun(incomingRunId);
              return;
            }
            if (active) return;
            if (incomingTask !== null) setTask(incomingTask);
            if (incomingMode !== null) setMode(incomingMode);
            if (incomingTask !== null || incomingMode !== null) {
              setTab("task");
              prompt.current?.focus();
            }
          }}
        />
      </Suspense>
      <div className="workspace-heading">
        <div>
          <span className="eyebrow">أفكار أوضح. خطوات قابلة للمراجعة.</span>
          <h1>مساحة العمل</h1>
        </div>
        <div className="heading-actions">
          <span className={`backend-indicator ${run.backend}`} role="status">
            <span />
            {run.backend === "checking"
              ? "جارٍ فحص الاتصال"
              : run.backend === "available"
                ? "الخلفية متصلة"
                : "تعذر الاتصال بالخلفية"}
          </span>
          {run.runId && (
            <button
              className="secondary-button"
              disabled={run.starting || active}
              onClick={newTask}
              title={
                active
                  ? "انتظر انتهاء المهمة؛ لا يتوفر إلغاء آمن من هذه الواجهة"
                  : undefined
              }
            >
              <Icon name="plus" width="16" />
              مهمة جديدة
            </button>
          )}
        </div>
      </div>
      <ExecutionDisclosure execution={run.execution} />
      <MissionStagesBar snapshot={run.snapshot} steps={steps} />
      <div className="workspace-grid">
        <div className="task-column">
          <div
            className="workspace-tabs"
            role="tablist"
            aria-label="أقسام مساحة العمل"
          >
            {tabs.map((item, index) => (
              <button
                key={item.id}
                id={`tab-${item.id}`}
                role="tab"
                aria-controls={`panel-${item.id}`}
                aria-selected={tab === item.id}
                tabIndex={tab === item.id ? 0 : -1}
                onClick={() => setTab(item.id)}
                onKeyDown={(event) => {
                  const direction =
                    event.key === "ArrowLeft"
                      ? 1
                      : event.key === "ArrowRight"
                        ? -1
                        : 0;
                  if (!direction && event.key !== "Home" && event.key !== "End")
                    return;
                  event.preventDefault();
                  const availableTabs =
                    window.innerWidth > 1100 ? tabs.slice(0, 2) : tabs;
                  const next =
                    event.key === "Home"
                      ? 0
                      : event.key === "End"
                        ? availableTabs.length - 1
                        : (index + direction + availableTabs.length) %
                          availableTabs.length;
                  setTab(availableTabs[next].id);
                  document
                    .getElementById(`tab-${availableTabs[next].id}`)
                    ?.focus();
                }}
              >
                {item.title}
                {item.id === "activity" && run.events.length > 0 && (
                  <span>{run.events.length}</span>
                )}
              </button>
            ))}
          </div>
          <div
            id="panel-task"
            role="tabpanel"
            aria-labelledby="tab-task"
            hidden={tab !== "task"}
          >
            {!run.runId && !run.starting && (
              <section className="welcome-panel">
                <div className="welcome-symbol">
                  <Icon name="spark" width="33" height="33" />
                  <span />
                </div>
                <span className="welcome-kicker">
                  مساحتك للعمل، لا لمجرد المحادثة
                </span>
                <h2>
                  فكرة تستحق
                  <br />
                  <span>خطوة إلى الأمام.</span>
                </h2>
                <p>
                  ابدأ بطلب، تابع ما ينفّذه الوكيل، وراجع النتيجة بنفسك.
                  <br className="desktop-break" /> كل خطوة واضحة، وكل نتيجة لها
                  سياق.
                </p>
                <div className="welcome-features">
                  <span>
                    <Icon name="layers" width="15" />
                    خطة واضحة
                  </span>
                  <span>
                    <Icon name="activity" width="15" />
                    نشاط قابل للتتبع
                  </span>
                  <span>
                    <Icon name="shield" width="15" />
                    نتائج دون مبالغة
                  </span>
                </div>
              </section>
            )}
            {run.runId && (
              <section className="panel task-overview">
                <div className="panel-heading">
                  <div>
                    <span className="eyebrow">المهمة الحالية</span>
                    <h2>طلبك ومسار تنفيذه</h2>
                  </div>
                  <StatusPill status={run.snapshot?.status ?? "running"} />
                </div>
                <p className="submitted-task">{run.snapshot?.task ?? task}</p>
                <div className={`conn conn-${run.connection}`} role="status">
                  <Icon name="activity" width="14" />
                  {connectionText}
                </div>
                <details className="task-reference">
                  <summary>مرجع المهمة</summary>
                  <code dir="ltr">{run.runId}</code>
                </details>
              </section>
            )}
            {run.starting && (
              <LoadingState>جارٍ إنشاء المهمة في الخادم…</LoadingState>
            )}
            {active && (
              <LoadingState>
                التنفيذ جارٍ؛ نعرض فقط التحديثات التي تصل من الخلفية.
              </LoadingState>
            )}
            {run.notice && <Notice>{run.notice}</Notice>}
            {run.backend === "unavailable" && !run.notice && (
              <Notice title="الخلفية غير متاحة">
                لم نتمكن من التحقق من اتصال الخادم. يمكنك إعادة تحميل الصفحة أو
                محاولة بدء المهمة لاحقًا.
              </Notice>
            )}
            {(run.snapshot?.pending_approvals ?? []).map((approval) => (
              <section
                className="panel approval-panel"
                key={approval.id}
                aria-label="موافقة مطلوبة"
              >
                <span className="eyebrow">القرار لك</span>
                <h2>إجراء ينتظر موافقتك</h2>
                <p>
                  طلب الخادم الموافقة على الأداة <code>{approval.name}</code>.
                  عدم الرد لا يعني الموافقة.
                </p>
                <p className="section-note">
                  لا نعرض وسائط الأداة الخام هنا. إذا لم تكن تفاصيل الإجراء
                  كافية، ارفضه بدل الموافقة دون معرفة آثاره.
                </p>
                <div className="button-row">
                  <button
                    className="primary-button"
                    disabled={Boolean(run.approving)}
                    onClick={() => void run.approve(approval.id, "approved")}
                  >
                    أوافق على الإجراء
                  </button>
                  <button
                    className="secondary-button"
                    disabled={Boolean(run.approving)}
                    onClick={() => void run.approve(approval.id, "denied")}
                  >
                    رفض الإجراء
                  </button>
                </div>
              </section>
            ))}
            {run.runId && <PlanPanel steps={steps} />}
            {run.snapshot?.final_answer && (
              <section
                className="panel result-panel"
                aria-label="نتيجة التنفيذ"
              >
                <div className="panel-heading">
                  <div>
                    <span className="eyebrow">ما أعاده المنفذ</span>
                    <h2>خرج المنفذ</h2>
                  </div>
                  <Icon name="file" />
                </div>
                <p className="verification-note">
                  حالة المحرك: <code>{run.snapshot.status}</code> — تحقيق الهدف
                  غير متحقق استقلاليًا.
                </p>
                {(run.snapshot.tool_results_summary?.unsuccessful ?? 0) > 0 && (
                  <p className="tool-result-warning" role="status">
                    <Icon name="alert" />
                    نتائج أدوات غير ناجحة:{" "}
                    {run.snapshot.tool_results_summary?.unsuccessful} — لا
                    تُعتبر هذه نتيجة ناجحة للطلب.
                  </p>
                )}
                <div className="answer" dir="auto">
                  {run.snapshot.final_answer}
                </div>
              </section>
            )}
            {run.snapshot?.error && (
              <Notice title="لم تكتمل المهمة">
                أبلغت الخلفية عن خطأ أو توقف في التنفيذ. لا توجد نتيجة مؤكدة؛
                راجع الحالة والنشاط قبل المحاولة مجددًا.
              </Notice>
            )}
            {run.snapshot?.status === "cancelled" && (
              <Notice title="أُلغيت المهمة">
                هذه حالة أبلغت بها الخلفية، وليست إلغاءً نفّذته الواجهة.
              </Notice>
            )}
            <form
              className="composer prompt-card"
              onSubmit={(event) => {
                event.preventDefault();
                void run.start(task, mode);
              }}
            >
              <label htmlFor="task-prompt">ما الذي تريد العمل عليه؟</label>
              <textarea
                ref={prompt}
                id="task-prompt"
                value={task}
                onChange={(event) => setTask(event.target.value)}
                rows={3}
                placeholder="اكتب طلبك هنا… وابدأ بخطوة واضحة"
                disabled={run.starting || active}
                onKeyDown={(event) => {
                  if (
                    event.key === "Enter" &&
                    (event.ctrlKey || event.metaKey) &&
                    !active
                  ) {
                    event.preventDefault();
                    void run.start(task, mode);
                  }
                }}
              />
              <div className="composer-toolbar">
                <div className="mode-control">
                  <Icon name="layers" width="16" />
                  <label htmlFor="execution-mode" className="sr-only">
                    نمط التنفيذ
                  </label>
                  <select
                    id="execution-mode"
                    value={mode}
                    onChange={(event) => setMode(event.target.value)}
                    disabled={run.starting || active}
                  >
                    <option value="auto">تلقائي</option>
                    <option value="react">ReAct · خطوات متتابعة</option>
                    <option value="flow">PlanningFlow · خطة مترابطة</option>
                  </select>
                </div>
                <button
                  className="primary-button"
                  type="submit"
                  disabled={run.starting || active || !task.trim()}
                >
                  <span>{run.starting ? "جارٍ البدء…" : "تشغيل ▶"}</span>
                  <Icon name="arrow" width="17" />
                </button>
              </div>
              <div className="composer-footnote">
                <span>
                  <Icon name="shield" width="13" />
                  لا ترسل أسرارًا أو بيانات شديدة الحساسية
                </span>
                <kbd dir="ltr">Ctrl ↵</kbd>
              </div>
            </form>
            {!run.runId && (
              <div className="suggestion-grid" aria-label="بدايات متاحة">
                <button
                  onClick={() =>
                    selectExample(
                      "افحص مشروعي، واكتشف نقاط الضعف، ثم أنشئ خطة نشر مجانية وآمنة",
                      "flow",
                    )
                  }
                >
                  <Icon name="shield" />
                  <span>
                    <strong>مهندس فحص المشاريع · Mission OS</strong>
                    <small>
                      فحص ساكن للملفات وتوليد تقارير قابلة للتنزيل (MD / JSON /
                      HTML) ببصمة SHA-256
                    </small>
                  </span>
                  <Icon name="arrow" width="16" />
                </button>
                <button onClick={() => selectExample("Sum 0..100", "react")}>
                  <Icon name="layers" />
                  <span>
                    <strong>جرّب خطوات التنفيذ</strong>
                    <small>سيناريو حساب تجريبي، وليس نموذجًا عامًا</small>
                  </span>
                  <Icon name="arrow" width="16" />
                </button>
                <button
                  onClick={() =>
                    selectExample(
                      "Plan a multi-step research comparison",
                      "flow",
                    )
                  }
                >
                  <Icon name="activity" />
                  <span>
                    <strong>استكشف خطة مترابطة</strong>
                    <small>سيناريو تجريبي متعدد الخطوات</small>
                  </span>
                  <Icon name="arrow" width="16" />
                </button>
              </div>
            )}
          </div>
          <div
            id="panel-activity"
            role="tabpanel"
            aria-labelledby="tab-activity"
            hidden={tab !== "activity"}
          >
            <ActivityPanel events={run.events} />
            {run.runId && (
              <button
                className="secondary-button reconnect"
                onClick={run.reconnect}
              >
                <Icon name="refresh" width="16" />
                إعادة اتصال البث
              </button>
            )}
            <p className="section-note">
              {connectionText}. انتهاء البث لا يثبت نجاح المهمة ولا فشلها.
            </p>
          </div>
          <div
            id="panel-evidence"
            role="tabpanel"
            aria-labelledby="tab-evidence"
            hidden={tab !== "evidence"}
            className="mobile-evidence"
          >
            <EvidencePanel snapshot={run.snapshot} runId={run.runId} />
          </div>
        </div>
        <div className="desktop-evidence">
          <EvidencePanel snapshot={run.snapshot} runId={run.runId} />
        </div>
      </div>
      <footer className="workspace-footer">
        <span>خطوات واضحة، ونتائج يمكنك مراجعتها.</span>
        <span dir="ltr">CELIA / WORKSPACE</span>
      </footer>
    </main>
  );
}
