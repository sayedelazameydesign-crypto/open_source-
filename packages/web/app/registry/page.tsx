"use client";

import { Icon } from "../../components/icon";
import {
  ConsoleHeading,
  ConsolePanel,
  CopyButton,
  DataOrigin,
  Fact,
  FactGrid,
  NotConfigured,
  RefreshButton,
} from "../../components/platform/console";
import { EmptyState, LoadingState, Notice, StatusPill } from "../../components/workspace/ui";
import { usePlatformInfo } from "../../hooks/use-platform-console";

/**
 * Image Registry — the build the process is running from.
 *
 * The values come from build args baked into the image by
 * `.github/workflows/docker-image.yml` (`CELA_IMAGE_REF`, `CELA_BUILD_SHA`,
 * `CELA_BUILD_TIME`). A locally built image without those args is reported as
 * unconfigured rather than guessed from a tag that "probably" exists.
 *
 * No pull, push, delete or tag action exists here: the browser has no registry
 * credentials, and pretending otherwise would be a fake control plane.
 */
export default function RegistryPage() {
  const platform = usePlatformInfo();
  const info = platform.data;
  const registry = info?.registry;
  const pull = registry?.reference ? `docker pull ${registry.reference}` : "";

  return (
    <main className="workspace-page console-page">
      <ConsoleHeading
        eyebrow="Image Registry · صورة الحاوية"
        title="الصورة التي يعمل منها التطبيق"
        lede={
          <>
            مرجع الصورة وبصمة البناء التي وُسمت داخل الحاوية وقت إنشائها. الشاشة{" "}
            <strong>لا تسحب ولا تدفع ولا تحذف</strong> أي صورة.
          </>
        }
      >
        <RefreshButton loading={platform.loading} onClick={platform.refresh} />
        <a className="secondary-button" href="/devbox">
          <Icon name="activity" width="15" /> بيئة التنفيذ
        </a>
      </ConsoleHeading>

      {platform.loading && !info && <LoadingState>جارٍ قراءة بيانات الصورة…</LoadingState>}
      {platform.error && <Notice title="تعذر قراءة بيانات الصورة">{platform.error}</Notice>}

      {registry && (
        <div className="workspace-grid console-grid">
          <div className="task-column">
            <ConsolePanel
              eyebrow="OCI Reference"
              title="المرجع الكامل"
              status={
                <StatusPill status={registry.configured ? "ready" : "awaiting_approval"}>
                  {registry.configured ? "مضبوط وقت البناء" : "غير مضبوط"}
                </StatusPill>
              }
            >
              {registry.configured && registry.reference ? (
                <>
                  <p className="registry-reference" dir="ltr">
                    {registry.reference}
                  </p>
                  <pre className="console-code" dir="ltr">
                    {pull}
                  </pre>
                  <CopyButton value={pull} label="نسخ أمر السحب" />
                  <FactGrid>
                    <Fact
                      label="المضيف"
                      value={registry.host ?? <NotConfigured>ضمن المرجع</NotConfigured>}
                      mono
                      ltr
                    />
                    <Fact
                      label="المستودع"
                      value={registry.repository ?? "—"}
                      mono
                      ltr
                    />
                    <Fact
                      label="الوسم"
                      value={registry.tag ?? <NotConfigured>بلا وسم صريح</NotConfigured>}
                      mono
                      ltr={Boolean(registry.tag)}
                    />
                    <Fact
                      label="البصمة (digest)"
                      value={
                        registry.digest ?? (
                          <NotConfigured>غير مثبتة — وسم متحرك</NotConfigured>
                        )
                      }
                      mono
                      ltr={Boolean(registry.digest)}
                      note="الوسم المتحرك قد يتغيّر؛ البصمة وحدها تثبّت البايتات."
                    />
                    <Fact
                      label="بصمة البناء"
                      value={
                        registry.build_sha ?? (
                          <NotConfigured>غير مضمّنة في البناء</NotConfigured>
                        )
                      }
                      mono
                      ltr={Boolean(registry.build_sha)}
                    />
                    <Fact
                      label="وقت البناء"
                      value={
                        registry.build_time ?? (
                          <NotConfigured>غير مضمّن في البناء</NotConfigured>
                        )
                      }
                      mono
                      ltr={Boolean(registry.build_time)}
                    />
                  </FactGrid>
                </>
              ) : (
                <EmptyState icon="layers" title="لم تُضبط بيانات الصورة" compact>
                  {registry.ignored_invalid_reference
                    ? "المتغيّر CELA_IMAGE_REF موجود لكنه ليس مرجع OCI صالحًا (أو يحمل بيانات اعتماد)، فتم تجاهله بدل عرضه."
                    : "بُني هذا التشغيل دون تمرير CELA_IMAGE_REF. املأ الحقول عبر build args في workflow بدل تخمين وسم."}
                </EmptyState>
              )}
              <DataOrigin>
                المصدر: متغيّرات مضمّنة في الصورة وقت البناء، تُقرأ من العملية. لا تُقرأ
                من السجل ولا من واجهة GHCR.
              </DataOrigin>
            </ConsolePanel>

            <ConsolePanel eyebrow="كيف تُملأ هذه الحقول" title="سلسلة البناء والنشر">
              <ol className="console-steps">
                <li>
                  <strong>بناء واختبار:</strong> الـ workflow يبني{" "}
                  <code dir="ltr">Dockerfile</code> ويشغّل فحصًا حيًا لـ{" "}
                  <code dir="ltr">/health</code> وبوابة المصادقة.
                </li>
                <li>
                  <strong>وسم:</strong> الدمج في <code dir="ltr">main</code> ينشر{" "}
                  <code dir="ltr">:latest</code>؛ فرع جلسة <code dir="ltr">arena/**</code>{" "}
                  ينشر <code dir="ltr">:edge</code>.
                </li>
                <li>
                  <strong>حقن الهوية:</strong> البناء يمرّر{" "}
                  <code dir="ltr">CELA_IMAGE_REF</code> و
                  <code dir="ltr">CELA_BUILD_SHA</code> و
                  <code dir="ltr">CELA_BUILD_TIME</code> كـ{" "}
                  <code dir="ltr">--build-arg</code>.
                </li>
                <li>
                  <strong>تشغيل:</strong> منصة الحاويات تسحب الصورة العامة وتشغّلها؛ لا
                  دور للمتصفح في السحب.
                </li>
              </ol>
            </ConsolePanel>
          </div>

          <aside className="evidence-column">
            <ConsolePanel eyebrow="حدود صريحة" title="ما لا يمكن التحقق منه هنا">
              <ul className="console-limits">
                <li>
                  لا نتحقق من صلاحية وصول الحزمة (عامة/خاصة) من هذه الصفحة؛ هذا إعداد
                  في GHCR وتُثبته عملية سحب ناجحة.
                </li>
                <li>
                  لا نعرض تاريخ نشر أو صحة الحاوية الحالية؛ مدة التشغيل في لوحة التشغيل
                  هي تقدير من إقلاع العملية.
                </li>
                <li>
                  لا سحب أو دفع أو حذف أو إعادة وسم. أي زر كهذا سيكون واجهة بلا وظيفة.
                </li>
              </ul>
            </ConsolePanel>

            <ConsolePanel eyebrow="إن ظهرت «غير مضبوط»" title="السبب المتوقع">
              <ul className="console-limits">
                <li>بناء محلي: <code dir="ltr">docker build .</code> بلا build args.</li>
                <li>صورة قديمة بُنيت قبل إضافة هذه الحقول.</li>
                <li>
                  منصة أعادت بناء الصورة من المستودع بنفسها دون تمرير الوسوم.
                </li>
              </ul>
              <DataOrigin>
                هذه الشاشة تصف، ولا تصلح تلقائيًا. إعادة البناء عبر الـ workflow هي
                الطريق الصحيح.
              </DataOrigin>
            </ConsolePanel>
          </aside>
        </div>
      )}
    </main>
  );
}
