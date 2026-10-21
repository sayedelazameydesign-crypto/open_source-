"use client";

import Link from "next/link";
import { useState } from "react";

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
import {
  probePlatformInfo,
  usePlatformInfo,
  type ProbeResult,
} from "../../hooks/use-platform-console";
import { formatUptime } from "../../lib/platform";

const MODE_LABELS: Record<string, string> = {
  disabled: "معطّل — يرفض كل طلب تنفيذ",
  local: "محلي — عملية فرعية بحدود موارد",
  e2b: "E2B microVM بعيد",
  firecracker: "Firecracker microVM بعيد",
  production: "إنتاجي — microVM بعيد فقط",
};

const ISOLATION_LABELS: Record<string, string> = {
  "subprocess-setrlimit": "عملية فرعية + setrlimit (ليست حدودًا كاملة)",
  "e2b-microvm": "microVM معزولة",
  "firecracker-microvm": "microVM معزولة",
  "remote-microvm": "microVM معزولة",
  none: "لا يوجد تنفيذ — لا حاجة لعزل",
  unknown: "غير معلوم (إعداد غير صالح)",
};

/**
 * Devbox — the execution environment, described and measured, not controlled.
 *
 * It deliberately offers no "run this snippet" box. Code execution is
 * disabled in the hosted image by design, and a browser-side runner would
 * either be a lie or a new untrusted-code surface; neither belongs in a
 * read-only console. What the page does offer is a *measured* round trip to
 * the API, so "the backend answers" is an observation with a number attached.
 */
export default function DevboxPage() {
  const platform = usePlatformInfo();
  const info = platform.data;
  const [probe, setProbe] = useState<ProbeResult | null>(null);
  const [probing, setProbing] = useState(false);

  async function runProbe() {
    setProbing(true);
    setProbe(await probePlatformInfo());
    setProbing(false);
  }

  const sandbox = info?.sandbox;

  return (
    <main className="workspace-page console-page">
      <ConsoleHeading
        eyebrow="Devbox · بيئة التنفيذ"
        title="بيئة التشغيل المعزولة"
        lede={
          <>
            نوع العزل وحدوده كما هي مضبوطة فعلًا في هذه العملية. لا نُدّعي عزلًا لم
            نتحقق منه، ولا نُشغّل كودًا من المتصفح.
          </>
        }
      >
        <RefreshButton loading={platform.loading} onClick={platform.refresh} />
        <button
          type="button"
          className="secondary-button"
          onClick={() => void runProbe()}
          disabled={probing}
        >
          <Icon name="activity" width="15" />
          {probing ? "جارٍ القياس…" : "قياس الاتصال بالخلفية"}
        </button>
        <Link className="secondary-button" href="/launchpad">
          <Icon name="grid" width="15" /> لوحة التشغيل
        </Link>
      </ConsoleHeading>

      {platform.loading && !info && <LoadingState>جارٍ قراءة إعدادات بيئة التنفيذ…</LoadingState>}
      {platform.error && <Notice title="تعذر قراءة بيئة التنفيذ">{platform.error}</Notice>}

      <div className="workspace-grid console-grid">
        <div className="task-column">
          {sandbox && (
            <ConsolePanel
              eyebrow="صندوق الرمل"
              title="الوضع الفعلي لتنفيذ الكود"
              status={
                <StatusPill
                  status={sandbox.executes_untrusted_code ? "awaiting_approval" : "ready"}
                >
                  {sandbox.executes_untrusted_code
                    ? "ينفّذ كودًا غير موثوق"
                    : "لا ينفّذ كودًا غير موثوق"}
                </StatusPill>
              }
            >
              <FactGrid>
                <Fact
                  label="وضع التنفيذ"
                  value={sandbox.code_exec_mode}
                  mono
                  ltr
                  note={MODE_LABELS[sandbox.code_exec_mode] ?? "قيمة غير معروفة للواجهة."}
                />
                <Fact
                  label="الخلفية المختارة"
                  value={
                    sandbox.backend ?? <NotConfigured>تعذر تحديدها من الإعداد</NotConfigured>
                  }
                  mono
                  ltr={Boolean(sandbox.backend)}
                />
                <Fact
                  label="حدّ العزل"
                  value={ISOLATION_LABELS[sandbox.isolation] ?? sandbox.isolation}
                />
                <Fact
                  label="عزل الشبكة"
                  value={
                    sandbox.network_isolation === "unverified"
                      ? "غير متحقق — لا يُدّعى"
                      : sandbox.network_isolation
                  }
                  mono
                  ltr={sandbox.network_isolation !== "unverified"}
                  note={
                    sandbox.network_isolation === "unverified"
                      ? "الخلفية المحلية تحاول netns وقد تفشل؛ الناتج يُبلَّغ لكل تنفيذ."
                      : undefined
                  }
                />
                <Fact
                  label="صورة الحاوية"
                  value={info?.registry.reference ?? <NotConfigured>غير مضبوطة</NotConfigured>}
                  mono
                  ltr={Boolean(info?.registry.reference)}
                />
                <Fact
                  label="الحدود الزمنية"
                  value="مهلة لكل تنفيذ + سقف خطوات/ميزانية"
                  note="تُفرض في طبقة الأدوات والمنصة، لا في الواجهة."
                />
              </FactGrid>
              {sandbox.configuration_error && (
                <Notice title="إعداد غير صالح">{sandbox.configuration_error}</Notice>
              )}
              <DataOrigin>
                المصدر: حقل <code dir="ltr">sandbox</code> في{" "}
                <code dir="ltr">GET /platform/info</code>. يصف الإعداد، ولا يُثبت أن
                كل تنفيذ نجح.
              </DataOrigin>
            </ConsolePanel>
          )}

          <ConsolePanel eyebrow="بيئة العملية" title="المنصّة واللغة">
            {info ? (
              <FactGrid>
                <Fact
                  label="Python"
                  value={`${info.process.python_version} (${info.process.python_implementation})`}
                  mono
                  ltr
                />
                <Fact
                  label="نظام التشغيل"
                  value={info.process.platform}
                  mono
                  ltr
                />
                <Fact
                  label="المعمارية"
                  value={info.process.machine ?? <NotConfigured>غير معلومة</NotConfigured>}
                  mono
                  ltr={Boolean(info.process.machine)}
                />
                <Fact
                  label="بدء العملية"
                  value={info.process.started_at}
                  mono
                  ltr
                  note={`مضى ${formatUptime(info.process.uptime_seconds)} — قيمة تقديرية.`}
                />
              </FactGrid>
            ) : (
              <EmptyState icon="activity" title="لا بيانات بيئة" compact>
                لم تصل حقائق العملية بعد؛ لا تُعرض قيم افتراضية.
              </EmptyState>
            )}
          </ConsolePanel>

          <ConsolePanel eyebrow="الأدوات المتاحة" title="ما يمكن للنموذج استدعاؤه">
            {info && info.tools.count > 0 ? (
              <>
                <p className="section-note">
                  {info.tools.count} أداة مسجّلة في هذه العملية. الاسم وحده لا يعني
                  نجاحًا: نتيجة كل استدعاء تُسجَّل منفصلة في مساحة العمل.
                </p>
                <ul className="tool-chips" aria-label="الأدوات المسجّلة">
                  {info.tools.registered.map((tool) => (
                    <li key={tool} dir="ltr">
                      {tool}
                    </li>
                  ))}
                </ul>
              </>
            ) : (
              <EmptyState icon="layers" title="لا أدوات مسجّلة" compact>
                لم تُعلن العملية عن أي أداة. هذا ليس دليل عطل، لكنه لا يسمح بادعاء قدرة.
              </EmptyState>
            )}
          </ConsolePanel>
        </div>

        <aside className="evidence-column">
          <ConsolePanel eyebrow="قياس حي" title="زمن الاستجابة عبر الوكيل">
            {probe ? (
              <>
                <FactGrid>
                  <Fact
                    label="نتيجة HTTP"
                    value={probe.status ?? "بلا استجابة"}
                    mono
                    ltr
                  />
                  <Fact label="الزمن" value={`${probe.ms} ms`} mono ltr />
                  <Fact label="الحجم" value={`${probe.bytes} bytes`} mono ltr />
                  <Fact
                    label="الوقت"
                    value={new Date(probe.at).toLocaleTimeString("ar-EG")}
                  />
                </FactGrid>
                <p className={`probe-note ${probe.ok ? "ok" : "bad"}`}>{probe.note}</p>
              </>
            ) : (
              <EmptyState icon="activity" title="لم يُقس بعد" compact>
                اضغط «قياس الاتصال بالخلفية» لطلب واحد مقيس عبر وكيل Next.js. القياس
                يثبت الوصول فقط، لا تشغيل أداة ولا تحقق هدف.
              </EmptyState>
            )}
            <DataOrigin>
              هذا القياس يُنفَّذ من متصفحك إلى نفس المسار <code dir="ltr">/api/backend</code>{" "}
              الذي تستخدمه الصفحات — لا اتصال مباشر بالخلفية ولا رموز في المتصفح.
            </DataOrigin>
          </ConsolePanel>

          <ConsolePanel eyebrow="حدود صريحة" title="لماذا لا يوجد محرّر كود">
            <ul className="console-limits">
              <li>
                صورة النشر تضبط <code dir="ltr">CELA_CODE_EXEC_MODE=disabled</code>؛ أي
                طلب تنفيذ يُرفض قبل أن يصل إلى المضيف.
              </li>
              <li>
                الإصدار المحلي يستخدم عملية فرعية بحدود موارد، وليست حدودًا كاملة ضد
                قراءة ملفات المضيف.
              </li>
              <li>
                لا يوجد في الـ API إجراء لإنشاء devbox أو تشغيل كود خام من المتصفح، فلا
                نعرض صندوقًا يوهم بذلك.
              </li>
            </ul>
          </ConsolePanel>
        </aside>
      </div>
    </main>
  );
}
