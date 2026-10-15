export type ExecutionInfo = {
  kind: "scripted" | "provider_adapter" | "unknown";
  model_name: string | null;
  goal_verification: "not_verified";
};

// Missing, legacy, and malformed metadata never imply a real model.
export function parseExecution(value: unknown): ExecutionInfo {
  if (value && typeof value === "object") {
    const data = value as Record<string, unknown>;
    if (data.kind === "scripted" || data.kind === "provider_adapter") {
      return {
        kind: data.kind,
        model_name:
          typeof data.model_name === "string" ? data.model_name : null,
        goal_verification: "not_verified",
      };
    }
  }
  return {
    kind: "unknown",
    model_name: null,
    goal_verification: "not_verified",
  };
}
export function ExecutionDisclosure({
  execution,
}: {
  execution: ExecutionInfo;
}) {
  const title =
    execution.kind === "scripted"
      ? "وضع تجريبي — نموذج محاكاة"
      : execution.kind === "provider_adapter"
        ? "موصل نموذج مهيأ — ليس تحققًا من هوية النموذج"
        : "نوع المنفذ غير متحقق";
  return (
    <section className="execution-disclosure" aria-label="حقيقة التشغيل">
      <details>
        <summary>
          <span className="disclosure-mark">i</span>
          <strong>{title}</strong>
          {execution.model_name && (
            <code dir="ltr">{execution.model_name}</code>
          )}
          <span className="disclosure-more">التفاصيل</span>
        </summary>
        <div className="disclosure-body">
          {execution.model_name && (
            <p>
              منفذ النموذج: <code>{execution.model_name}</code>
            </p>
          )}
          {execution.kind === "scripted" && (
            <p>
              إجابات النموذج معدّة مسبقًا؛ بعض الأدوات محاكاة. الطلب لا يضمن
              سلوكًا عامًا أو تصفحًا حقيقيًا.
            </p>
          )}
          <p>وجهة التوجيه المقترحة ليست دليلًا على استدعاء ذلك النموذج.</p>
          <p>
            اكتمال دورة المحرك لا يثبت تحقيق هدف الطلب؛ التحقق المستقل من الهدف
            غير متاح هنا.
          </p>
        </div>
      </details>
    </section>
  );
}
