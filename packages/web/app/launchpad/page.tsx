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
} from "../../components/workspace/ui";
import { useLedgerReport, usePlatformInfo } from "../../hooks/use-platform-console";
import { formatDuration, formatPercent, formatUptime } from "../../lib/platform";

/**
 * App Launchpad — a read-only window onto the running deployment.
 *
 * There is deliberately no "deploy" / "restart" / "scale" button: this
 * backend exposes no such action, and a button that cannot work is worse than
 * no button. Deployments happen through the GHCR image workflow and the
 * hosting console; this page only states what is actually running.
 */
export default function LaunchpadPage() {
  const platform = usePlatformInfo();
  const ledger = useLedgerReport(5);
  const info = platform.data;
  const recent = ledger.data;

  return (
    <main className="workspace-page console-page">
      <ConsoleHeading
        eyebrow="App Launchpad · ما يعمل الآن"
        title="التطبيق المنشور"
        lede={
          <>
            حقائق مقروءة من عملية الخادم نفسها: الهوية، وضع التنفيذ، المخزن، والصورة.
            هذه الشاشة <strong>لا تنشر ولا تعيد التشغيل</strong> — لا يوجد في الـ API
            إجراء لذلك، فلا يُعرض زر وهمي.
          </>
        }
      >
        <RefreshButton
          loading={platform.loading}
          onClick={() => {
            platform.refresh();
            ledger.refresh();
          }}
        />
        <a className="secondary-button" href="/status">
          <Icon name="shield" width="15" /> حالة المنصة
        </a>
      </ConsoleHeading>

      {platform.loading && !info && <LoadingState>جارٍ قراءة حقائق التشغيل…</LoadingState>}
      {platform.error && (
        <Notice title="تعذر قراءة حقائق التشغيل">{platform.error}</Notice>
      )}

      {info && (
        <div className="workspace-grid console-grid">
          <div className="task-column">
            <ConsolePanel
              eyebrow="الهوية والإصدار"
              title={info.deployment.app_name}
              status={
                <StatusPill
                  status={info.deployment.mode === "live" ? "running" : "awaiting_approval"}
                >
                  {info.deployment.mode === "live"
                    ? "مزوّد حقيقي (live)"
                    : "وضع تجريبي (scripted)"}
                </StatusPill>
              }
            >
              <FactGrid>
                <Fact label="المنتج" value={info.product} />
                <Fact label="الإصدار" value={info.version} mono ltr />
                <Fact
                  label="مدة التشغيل"
                  value={formatUptime(info.process.uptime_seconds)}
                  mono
                  ltr
                  note="منذ إقلاع العملية؛ قيمة وصفية لا تُثبت استقرارًا."
                />
                <Fact
                  label="منفذ الاستماع"
                  value={
                    info.deployment.listen_port ?? (
                      <NotConfigured>غير معلوم</NotConfigured>
                    )
                  }
                  mono
                  ltr
                />
                <Fact
                  label="المنطقة"
                  value={info.deployment.region ?? <NotConfigured>غير مضبوطة</NotConfigured>}
                  mono
                  ltr={Boolean(info.deployment.region)}
                />
                <Fact
                  label="عنوان عام مضبوط"
                  value={
                    info.deployment.public_url_configured ? (
                      "نعم (المتغيّر موجود فقط)"
                    ) : (
                      <NotConfigured>لا — لا يُستنتج العنوان من الطلب</NotConfigured>
                    )
                  }
                />
              </FactGrid>
              <DataOrigin>
                المصدر: <code dir="ltr">GET /platform/info</code> من العملية الحيّة. لا
                تُقرأ متغيّرات قد تحمل أسرارًا؛ ولا يُعاد أي رمز أو كلمة مرور.
              </DataOrigin>
            </ConsolePanel>

            <ConsolePanel eyebrow="التنفيذ والمخزن" title="ما يُشغّل المهام ويحفظها">
              <FactGrid>
                <Fact
                  label="هوية المنفّذ"
                  value={info.execution.kind}
                  mono
                  ltr
                  note="scripted = نموذج تجريبي حتمي؛ provider_adapter = مزوّد خارجي مضبوط."
                />
                <Fact
                  label="اسم النموذج"
                  value={
                    info.execution.model_name || (
                      <NotConfigured>غير معلن من المنفّذ</NotConfigured>
                    )
                  }
                  mono
                  ltr={Boolean(info.execution.model_name)}
                />
                <Fact
                  label="تحقق الهدف"
                  value={info.execution.goal_verification}
                  mono
                  ltr
                  note="لا يُستنتج تحقق الهدف من نجاح دورة المحرك."
                />
                <Fact
                  label="مخزن المهام"
                  value={`${info.store.backend} · ${info.store.scope}`}
                  mono
                  ltr
                  note={info.store.durable ? "دائم عبر إعادة التشغيل." : "في الذاكرة فقط — يزول مع العملية."}
                />
                <Fact
                  label="أدوات مسجّلة"
                  value={String(info.tools.count)}
                  mono
                  ltr
                  note={info.tools.registered.slice(0, 6).join(", ")}
                />
                <Fact
                  label="سجل النتائج"
                  value={
                    info.store.ledger.enabled ? (
                      `${info.store.ledger.entries ?? 0} مدخلًا`
                    ) : (
                      <NotConfigured>معطّل (CELA_LEDGER_PATH=off)</NotConfigured>
                    )
                  }
                  note={info.store.ledger.path}
                />
              </FactGrid>
              <div className="console-links">
                <a className="secondary-button" href="/devbox">
                  <Icon name="activity" width="15" /> بيئة التنفيذ
                </a>
                <a className="secondary-button" href="/database">
                  <Icon name="folder" width="15" /> المخزن والسجل
                </a>
                <a className="secondary-button" href="/registry">
                  <Icon name="layers" width="15" /> صورة الحاوية
                </a>
              </div>
            </ConsolePanel>
          </div>

          <aside className="evidence-column">
            <ConsolePanel
              eyebrow="آخر النتائج"
              title="سجل التشغيل"
              status={
                recent?.summary ? (
                  <StatusPill status="ready">
                    {formatPercent(recent.summary.success_rate)} نجاح
                  </StatusPill>
                ) : undefined
              }
            >
              {ledger.loading && !recent && <LoadingState>جارٍ قراءة السجل…</LoadingState>}
              {ledger.error && (
                <Notice title="تعذر قراءة سجل النتائج">{ledger.error}</Notice>
              )}
              {recent && recent.entries.length === 0 && (
                <EmptyState icon="activity" title="لا نتائج مسجّلة بعد" compact>
                  لم يُنفَّذ أي تشغيل على هذه الخلفية بعد، أو أن السجل معطّل. الصفر هنا
                  حقيقي وليس تقديرًا.
                </EmptyState>
              )}
              {recent && recent.entries.length > 0 && (
                <ul className="artifact-list console-run-list">
                  {recent.entries.map((entry) => (
                    <li key={`${entry.seq}-${entry.run_id}`}>
                      <span className="file-icon">
                        <Icon name="activity" width="16" />
                      </span>
                      <div>
                        <strong>{entry.task || "مهمة بلا نص"}</strong>
                        <small>
                          <StatusPill status={entry.status} />{" "}
                          · {formatDuration(entry.duration_ms)} ·{" "}
                          <span dir="ltr">{entry.mode}</span>
                        </small>
                      </div>
                    </li>
                  ))}
                </ul>
              )}
              {recent && recent.summary && (
                <p className="console-origin">
                  الإجمالي {recent.summary.total} تشغيلًا · p50{" "}
                  {formatDuration(recent.summary.duration_ms.p50)} · p95{" "}
                  {formatDuration(recent.summary.duration_ms.p95)}
                </p>
              )}
              <DataOrigin>
                المصدر: <code dir="ltr">GET /ledger?limit=5</code> — سطر دائم لكل تنفيذ،
                بما فيه الفاشل والمقطوع.
              </DataOrigin>
            </ConsolePanel>

            <ConsolePanel eyebrow="حدود صريحة" title="ما لا تفعله هذه الشاشة">
              <ul className="console-limits">
                <li>لا إنشاء تطبيق ولا نشره ولا إعادة تشغيله من المتصفح.</li>
                <li>لا تعديل للموارد أو المتغيّرات أو النطاق.</li>
                <li>
                  النشر الفعلي يمر عبر <code dir="ltr">Actions → GHCR → منصة الاستضافة</code>{" "}
                  كما هو موثّق في دليل النشر.
                </li>
              </ul>
            </ConsolePanel>
          </aside>
        </div>
      )}
    </main>
  );
}
