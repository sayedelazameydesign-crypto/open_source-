"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { Icon } from "../../components/icon";
import { LoadingState, Notice, StatusPill } from "../../components/workspace/ui";
import { object } from "../../lib/contracts";

type MissionStatusPayload = {
  product: string;
  version: string;
  core_capabilities: {
    deterministic_inspector_core: {
      active: boolean;
      requires_api_key: boolean;
      tools: string[];
    };
    ssrf_and_egress_guard: {
      active: boolean;
      blocks_private_ip_and_metadata: boolean;
    };
    durable_run_store: {
      active: boolean;
      backend: string;
    };
    verified_report_downloads: {
      active: boolean;
      formats: string[];
      sha256_verified: boolean;
    };
  };
  optional_integrations: {
    llm_provider: {
      configured: boolean;
      provider_name: string | null;
      fallback_mode: string;
    };
    activepieces_webhook: {
      configured: boolean;
      quota: {
        daily_used: number;
        daily_limit: number;
        monthly_used: number;
        monthly_limit: number;
      };
    };
  };
};

export default function StatusPage() {
  const [status, setStatus] = useState<MissionStatusPayload | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  useEffect(() => {
    const ctrl = new AbortController();
    (async () => {
      try {
        const res = await fetch("/api/backend/mission/status", {
          signal: ctrl.signal,
        });
        const data: unknown = await res.json();
        if (!res.ok || !object(data) || data.status !== "ok") {
          throw new Error("invalid_status");
        }
        if (!ctrl.signal.aborted) {
          setStatus(data as unknown as MissionStatusPayload);
          setLoading(false);
        }
      } catch {
        if (!ctrl.signal.aborted) {
          setError(
            "تعذر جلب حالة المنصة الحية من الخلفية. لا نفترض جاهزية الخادم عند انقطاع الاتصال.",
          );
          setLoading(false);
        }
      }
    })();
    return () => ctrl.abort();
  }, []);

  return (
    <main className="workspace-page">
      <div className="workspace-heading">
        <div>
          <span className="eyebrow">الشفافية التشغيلية · Live Platform Status</span>
          <h1>حالة المنصة الحية</h1>
        </div>
        <div className="heading-actions">
          <Link className="secondary-button" href="/launchpad">
            <Icon name="grid" width="15" />
            App Launchpad
          </Link>
          <Link className="secondary-button" href="/">
            <Icon name="arrow" width="15" />
            العودة إلى مساحة العمل
          </Link>
        </div>
      </div>

      {loading && <LoadingState>جارٍ فحص حالة المنصة الحية…</LoadingState>}
      {error && <Notice title="تعذر التحقق من الحالة">{error}</Notice>}

      {status && (
        <div className="workspace-grid">
          <div className="task-column">
            <section className="panel">
              <div className="panel-heading">
                <div>
                  <span className="eyebrow">قدرات أساسية بلا مفاتيح خارجية</span>
                  <h2>النواة الحتمية والأمان ({status.product})</h2>
                </div>
                <StatusPill status="ready">فعّالة محليًا</StatusPill>
              </div>
              <ul className="artifact-list">
                <li>
                  <span className="file-icon">
                    <Icon name="shield" />
                  </span>
                  <div>
                    <strong>نواة مهندس فحص المشاريع (Project Inspector Core)</strong>
                    <small>
                      تعمل بصفر مفاتيح API خارجية · الأدوات المسجلة:{" "}
                      <code dir="ltr">
                        {status.core_capabilities.deterministic_inspector_core.tools.join(
                          ", ",
                        )}
                      </code>
                    </small>
                  </div>
                </li>
                <li>
                  <span className="file-icon">
                    <Icon name="shield" />
                  </span>
                  <div>
                    <strong>حارس SSRF وسياسة الخروج (EgressPolicy)</strong>
                    <small>
                      يمنع العناوين الداخلية و169.254.169.254 وإعادة التوجيه غير الآمنة
                    </small>
                  </div>
                </li>
                <li>
                  <span className="file-icon">
                    <Icon name="folder" />
                  </span>
                  <div>
                    <strong>التخزين الدائم للمهام (RunStore)</strong>
                    <small>
                      المحرّك النشط:{" "}
                      <code dir="ltr">
                        {status.core_capabilities.durable_run_store.backend}
                      </code>
                    </small>
                  </div>
                </li>
                <li>
                  <span className="file-icon">
                    <Icon name="file" />
                  </span>
                  <div>
                    <strong>تنزيل التقارير الموثّقة ببصمة SHA-256</strong>
                    <small>
                      الصيغ المدعومة:{" "}
                      <code dir="ltr">
                        {status.core_capabilities.verified_report_downloads.formats
                          .join(" / ")
                          .toUpperCase()}
                      </code>
                    </small>
                  </div>
                </li>
              </ul>
            </section>

            <section className="panel">
              <div className="panel-heading">
                <div>
                  <span className="eyebrow">تكاملات اختيارية (0$ Free Tier)</span>
                  <h2>مزوّد النموذج والأتمتة الخارجية</h2>
                </div>
                <Icon name="layers" />
              </div>
              <ul className="artifact-list">
                <li>
                  <span className="file-icon">
                    <Icon name="spark" />
                  </span>
                  <div>
                    <strong>مزوّد الذكاء الاصطناعي الاختياري (Gemini / Groq / OpenRouter)</strong>
                    <small>
                      {status.optional_integrations.llm_provider.configured
                        ? `مفعّل (${status.optional_integrations.llm_provider.provider_name}) مع تراجع تلقائي للنواة الحتمية عند نفاد الحصة`
                        : "غير مضاف حاليًا — يعمل النظام عبر النواة الحتمية المحلية (deterministic_mission_core) دون توقف"}
                    </small>
                  </div>
                </li>
                <li>
                  <span className="file-icon">
                    <Icon name="activity" />
                  </span>
                  <div>
                    <strong>موصّل Activepieces الموقّع (HMAC + حارس الحصة المجانية)</strong>
                    <small>
                      {status.optional_integrations.activepieces_webhook.configured
                        ? `مفعّل · الاستهلاك اليومي: ${status.optional_integrations.activepieces_webhook.quota.daily_used}/${status.optional_integrations.activepieces_webhook.quota.daily_limit}`
                        : "غير مفعّل (اختياري) · سقف الحماية اليومي الافتراضي: 100 مهمة/يوم"}
                    </small>
                  </div>
                </li>
              </ul>
            </section>
          </div>
        </div>
      )}
    </main>
  );
}
