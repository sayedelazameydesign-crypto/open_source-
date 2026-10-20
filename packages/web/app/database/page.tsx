"use client";

import { Icon } from "../../components/icon";
import {
  ConsoleHeading,
  ConsolePanel,
  DataOrigin,
  Fact,
  FactGrid,
  NotConfigured,
  RefreshButton,
} from "../../components/platform/console";
import {
  EmptyState,
  LoadingState,
  Notice,
  StatusPill,
  statusLabels,
} from "../../components/workspace/ui";
import { useLedgerReport, usePlatformInfo } from "../../hooks/use-platform-console";
import {
  formatDuration,
  formatPercent,
  type LedgerSummary,
} from "../../lib/platform";

const OUTCOME_LABELS: Record<string, string> = {
  success: "نجاح",
  failed: "فشل",
  budget: "توقف بالميزانية",
  aborted: "مقطوع",
};

function SummaryPanel({ summary }: { summary: LedgerSummary }) {
  return (
    <ConsolePanel
      eyebrow="ملخّص السجل"
      title="كيف انتهت التشغيلات"
      status={<StatusPill status="ready">{summary.total} تشغيلًا</StatusPill>}
    >
      <FactGrid>
        <Fact
          label="معدّل النجاح"
          value={formatPercent(summary.success_rate)}
          mono
          ltr
          note="نجاح = إنتاج جواب نهائي، وليس تحقق هدف المستخدم."
        />
        <Fact
          label="المدة p50 / p95"
          value={`${formatDuration(summary.duration_ms.p50)} / ${formatDuration(summary.duration_ms.p95)}`}
          mono
          ltr
        />
        <Fact
          label="استدعاءات الأدوات"
          value={`${summary.totals.tool_calls} (فشل ${summary.totals.tool_failures})`}
          mono
          ltr
        />
        <Fact
          label="الخطوات / إعادة التخطيط"
          value={`${summary.totals.steps_used} / ${summary.totals.replans}`}
          mono
          ltr
        />
        <Fact
          label="أسطر تالفة"
          value={String(summary.corrupt_lines)}
          mono
          ltr
          note="ملفات JSONL قد تتعرض لكتابة جزئية؛ تُعدّ ولا تُعتبر نتائج."
        />
        <Fact
          label="أول / آخر تسجيل"
          value={`${summary.first_recorded_at ?? "—"} → ${summary.last_recorded_at ?? "—"}`}
          mono
          ltr
          note="بتوقيت UTC كما كتبه الخادم."
        />
      </FactGrid>
      <div className="outcome-bars" aria-label="توزيع النتائج">
        {Object.entries(summary.outcomes).map(([outcome, count]) => (
          <span key={outcome} className={`outcome-chip ${outcome}`}>
            {OUTCOME_LABELS[outcome] ?? outcome}: <strong>{count}</strong>
          </span>
        ))}
      </div>
      <DataOrigin>
        المصدر: <code dir="ltr">GET /ledger</code> — يُقرأ الملف عند كل طلب، ويشمل كل
        تنفيذ نجح أو فشل أو قُطع.
      </DataOrigin>
    </ConsolePanel>
  );
}

export default function DatabasePage() {
  const platform = usePlatformInfo();
  const ledger = useLedgerReport(20);
  const info = platform.data;
  const report = ledger.data;
  const store = info?.store;

  return (
    <main className="workspace-page console-page">
      <ConsoleHeading
        eyebrow="Database · المخزن والسجل"
        title="أين تُحفظ حالة المهام"
        lede={
          <>
            المخزن الفعلي الذي تستخدمه هذه العملية وسجل النتائج المرتبط به. الشاشة
            للقراءة فقط: <strong>لا SQL، ولا ترحيل، ولا حذف</strong>.
          </>
        }
      >
        <RefreshButton
          loading={platform.loading || ledger.loading}
          onClick={() => {
            platform.refresh();
            ledger.refresh();
          }}
        />
        <a className="secondary-button" href="/launchpad">
          <Icon name="grid" width="15" /> لوحة التشغيل
        </a>
      </ConsoleHeading>

      {platform.error && <Notice title="تعذر قراءة إعداد المخزن">{platform.error}</Notice>}
      {ledger.error && <Notice title="تعذر قراءة سجل النتائج">{ledger.error}</Notice>}
      {ledger.loading && !report && <LoadingState>جارٍ قراءة سجل النتائج…</LoadingState>}

      <div className="workspace-grid console-grid">
        <div className="task-column">
          {store && (
            <ConsolePanel
              eyebrow="RunStore"
              title={store.backend}
              status={
                <StatusPill status={store.durable ? "ready" : "awaiting_approval"}>
                  {store.durable ? "حفظ دائم" : "في الذاكرة فقط"}
                </StatusPill>
              }
            >
              <FactGrid>
                <Fact label="النطاق" value={store.scope} mono ltr />
                <Fact
                  label="الهدف"
                  value={
                    store.target ?? <NotConfigured>لا هدف مضبوط (مخزن في الذاكرة)</NotConfigured>
                  }
                  mono
                  ltr={Boolean(store.target)}
                  note="تُحجب بيانات الاعتماد ومعاملات الاستعلام قبل العرض."
                />
                <Fact
                  label="مدة البقاء"
                  value={
                    store.durable
                      ? "تبقى الحالة واللقطات والملفات بعد إعادة التشغيل."
                      : "تزول الحالة عند إعادة تشغيل العملية."
                  }
                />
                <Fact
                  label="سجل النتائج"
                  value={store.ledger.enabled ? "مفعّل" : "معطّل"}
                />
                <Fact
                  label="مسار السجل"
                  value={store.ledger.path ?? <NotConfigured>—</NotConfigured>}
                  mono
                  ltr={Boolean(store.ledger.path)}
                  note={
                    store.ledger.schema_version
                      ? `مخطط الإصدار ${store.ledger.schema_version}`
                      : undefined
                  }
                />
                <Fact
                  label="مدخلات السجل"
                  value={
                    store.ledger.entries === undefined
                      ? "—"
                      : String(store.ledger.entries)
                  }
                  mono
                  ltr
                />
              </FactGrid>
              <DataOrigin>
                المصدر: حقل <code dir="ltr">store</code> في{" "}
                <code dir="ltr">GET /platform/info</code>، مشتق من الكائن الحي لا من
                متغيّر بيئة.
              </DataOrigin>
            </ConsolePanel>
          )}

          <ConsolePanel eyebrow="آخر التشغيلات" title="سطور السجل الخام" >
            {report && report.entries.length === 0 && (
              <EmptyState icon="folder" title="السجل فارغ" compact>
                لا سطور مسجّلة. إن كان السجل معطّلًا فهذا متوقع، وليس مؤشر نجاح.
              </EmptyState>
            )}
            {report && report.entries.length > 0 && (
              <div className="console-table-wrap">
                <table className="console-table">
                  <caption className="sr-only">
                    آخر تشغيلات مسجّلة بحالتها ومدتها وأدواتها
                  </caption>
                  <thead>
                    <tr>
                      <th scope="col">#</th>
                      <th scope="col">المهمة</th>
                      <th scope="col">الحالة</th>
                      <th scope="col">المدة</th>
                      <th scope="col">خطوات</th>
                      <th scope="col">أدوات</th>
                    </tr>
                  </thead>
                  <tbody>
                    {report.entries.map((entry) => (
                      <tr key={`${entry.seq}-${entry.run_id}`}>
                        <td dir="ltr">{entry.seq}</td>
                        <td>
                          <span className="table-task">{entry.task || "بلا نص"}</span>
                          <small dir="ltr">{entry.run_id}</small>
                        </td>
                        <td>
                          <StatusPill status={entry.status}>
                            {statusLabels[entry.status] ?? entry.status}
                          </StatusPill>
                        </td>
                        <td dir="ltr">{formatDuration(entry.duration_ms)}</td>
                        <td dir="ltr">{entry.steps_used}</td>
                        <td dir="ltr">
                          {entry.tool_calls}
                          {entry.tool_failures > 0 ? ` / فشل ${entry.tool_failures}` : ""}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </ConsolePanel>
        </div>

        <aside className="evidence-column">
          {report?.summary ? (
            <SummaryPanel summary={report.summary} />
          ) : (
            <ConsolePanel eyebrow="ملخّص السجل" title="لا ملخص بعد">
              <EmptyState icon="activity" title="لم يصل ملخص" compact>
                الملخص يصل مع قراءة السجل؛ لا نعرض أرقامًا محسوبة محليًا بدلًا منه.
              </EmptyState>
            </ConsolePanel>
          )}

          <ConsolePanel eyebrow="الاستمرارية" title="كيف يصبح المخزن دائمًا">
            <ul className="console-limits">
              <li>
                الافتراضي في الحاويات <code dir="ltr">InMemoryRunStore</code>؛ التطبيق
                stateless، فأي إعادة تشغيل تُفقد المهام الجارية.
              </li>
              <li>
                للحفظ الدائم اضبط <code dir="ltr">CELA_RUN_STORE_URL</code> على SQLite
                لقرص ثابت، أو على رابط Postgres (Supabase Session pooler) في النشر
                السحابي — كما في دليل النشر.
              </li>
              <li>
                يُطبّق المخطط عند أول استخدام فقط. لا تُنفّذ هذه الصفحة أي ترحيل أو
                تنظيف.
              </li>
            </ul>
            <DataOrigin>
              لا تُعرض كلمة مرور قاعدة البيانات في أي استجابة؛ استخدم{" "}
              <code dir="ltr">scrub_credentials</code> على الخادم قبل العرض (مطبّق
              بالفعل).
            </DataOrigin>
          </ConsolePanel>
        </aside>
      </div>
    </main>
  );
}
