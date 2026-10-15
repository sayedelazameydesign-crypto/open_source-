"use client";

import { usePathname } from "next/navigation";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { Icon } from "./icon";

function Navigation({ onNavigate }: { onNavigate?: () => void }) {
  const pathname = usePathname();
  return (
    <>
      <a
        className="brand"
        href="/"
        aria-label="CELIA — مساحة العمل"
        onClick={onNavigate}
      >
        <span className="brand-symbol">
          <Icon name="spark" width="25" height="25" />
        </span>
        <span className="brand-word" dir="ltr">
          CELIA<span>WORKSPACE</span>
        </span>
      </a>
      <div className="nav-label">مساحتك</div>
      <nav aria-label="التنقل الرئيسي" className="primary-nav">
        <a
          href="/"
          aria-current={pathname === "/" ? "page" : undefined}
          onClick={onNavigate}
        >
          <Icon name="grid" /> مساحة العمل{" "}
          <span className="nav-active-marker" />
        </a>
        <a
          href="/documents"
          aria-current={pathname === "/documents" ? "page" : undefined}
          onClick={onNavigate}
        >
          <Icon name="file" /> المستندات النصية
        </a>
      </nav>
      <div className="sidebar-note">
        <span className="small-icon">
          <Icon name="layers" />
        </span>
        <h2>من فكرة إلى نتيجة واضحة</h2>
        <p>
          طلبك، خطوات التنفيذ، والأدلة في مساحة واحدة. دون وعود تتجاوز ما نُفّذ.
        </p>
        <a href="/documents" onClick={onNavigate}>
          جرّب معالجة ملف TXT <Icon name="arrow" width="15" />
        </a>
      </div>
      <div className="sidebar-footer">
        <span className="local-avatar">
          <Icon name="shield" />
        </span>
        <div>
          <strong>جلسة محلية</strong>
          <span>مستخدم واحد · ليست خدمة إنتاجية</span>
        </div>
      </div>
    </>
  );
}

export function AppShell({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const [menuOpen, setMenuOpen] = useState(false);
  const dialog = useRef<HTMLDialogElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    const resize = () => {
      if (window.innerWidth > 760) dialog.current?.close();
    };
    window.addEventListener("resize", resize);
    return () => window.removeEventListener("resize", resize);
  }, []);
  const title =
    pathname === "/documents"
      ? "المستندات النصية"
      : pathname === "/"
        ? "مساحة العمل"
        : "أدوات المشروع";
  return (
    <div className="app-frame">
      <a className="skip-link" href="#main-content">
        تخطّ إلى المحتوى
      </a>
      <aside className="desktop-sidebar">
        <Navigation />
      </aside>
      <dialog
        ref={dialog}
        className="mobile-drawer"
        aria-label="قائمة التنقل"
        onKeyDown={(event) => {
          if (event.key !== "Tab") return;
          const items = Array.from(
            event.currentTarget.querySelectorAll<HTMLElement>(
              "a[href], button:not([disabled])",
            ),
          );
          const first = items[0];
          const last = items[items.length - 1];
          if (event.shiftKey && document.activeElement === first) {
            event.preventDefault();
            last?.focus();
          } else if (!event.shiftKey && document.activeElement === last) {
            event.preventDefault();
            first?.focus();
          }
        }}
        onCancel={(event) => {
          event.preventDefault();
          dialog.current?.close();
        }}
        onClose={() => {
          setMenuOpen(false);
        }}
      >
        <button
          className="icon-button drawer-close"
          aria-label="إغلاق التنقل"
          onClick={() => dialog.current?.close()}
        >
          <Icon name="close" />
        </button>
        <Navigation onNavigate={() => dialog.current?.close()} />
      </dialog>
      <div className="app-main">
        <header className="app-topbar">
          <div className="breadcrumb">
            <button
              ref={trigger}
              className="icon-button mobile-menu"
              aria-label="فتح التنقل"
              aria-haspopup="dialog"
              aria-expanded={menuOpen}
              onClick={() => {
                dialog.current?.showModal();
                setMenuOpen(true);
              }}
            >
              <Icon name="menu" />
            </button>
            <span className="breadcrumb-brand" dir="ltr">
              CELIA
            </span>
            <span className="crumb-divider">/</span>
            <span>{title}</span>
          </div>
          <span className="topbar-note">
            <span className="static-dot" />
            بيئة تطوير محلية
          </span>
        </header>
        <div id="main-content" tabIndex={-1} className="app-content">
          {children}
        </div>
      </div>
    </div>
  );
}
