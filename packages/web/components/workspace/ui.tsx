import type { ReactNode } from "react";
import { Icon, type IconName } from "../icon";

export const statusLabels: Record<string, string> = {
  pending: "بانتظار التنفيذ",
  running: "قيد التنفيذ",
  awaiting_approval: "بانتظار موافقتك",
  completed: "انتهت دورة المحرك",
  failed: "فشل التنفيذ",
  cancelled: "أُلغيت المهمة",
  max_steps: "توقف عند الحد المسموح",
  done: "انتهت الخطوة",
  error: "فشلت الأداة",
  success: "نجحت الأداة",
  denied: "رُفض الإجراء",
  timeout: "انتهت المهلة",
  ready: "جاهزة",
  skipped: "تم تخطيها",
};
export function StatusPill({
  status,
  children,
}: {
  status: string;
  children?: ReactNode;
}) {
  const tone = ["failed", "error", "denied", "max_steps", "timeout"].includes(
    status,
  )
    ? "danger"
    : ["running", "awaiting_approval"].includes(status)
      ? "amber"
      : "neutral";
  return (
    <span className={`status-pill ${tone}`}>
      <span className="pill-dot" />
      {children ??
        (Object.hasOwn(statusLabels, status)
          ? statusLabels[status]
          : "حالة غير معروفة")}
    </span>
  );
}
export function EmptyState({
  icon = "layers",
  title,
  children,
  compact = false,
}: {
  icon?: IconName;
  title: string;
  children: ReactNode;
  compact?: boolean;
}) {
  return (
    <div className={`empty-state${compact ? " compact" : ""}`}>
      <span className="empty-icon">
        <Icon name={icon} width="24" height="24" />
      </span>
      <h3>{title}</h3>
      <p>{children}</p>
    </div>
  );
}
export function Notice({
  children,
  title = "تحتاج إلى انتباه",
}: {
  children: ReactNode;
  title?: string;
}) {
  return (
    <div className="notice" role="alert">
      <Icon name="alert" />
      <div>
        <strong>{title}</strong>
        <p>{children}</p>
      </div>
    </div>
  );
}
export function LoadingState({ children }: { children: ReactNode }) {
  return (
    <div className="loading-state" role="status">
      <span className="loading-spinner" />
      {children}
    </div>
  );
}
