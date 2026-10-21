"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { Icon } from "./icon";
import { AccessGate } from "./access-gate";
import {
  CONSOLE_NAV_ITEMS,
  WORKSPACE_NAV_ITEMS,
  resolvePageTitle,
} from "../lib/page-links";

function Navigation({ onNavigate }: { onNavigate?: () => void }) {
  const pathname = usePathname();
  return (
    <>
      <Link
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
      </Link>
      <div className="nav-label">مساحتك</div>
      <nav aria-label="التنقل الرئيسي" className="primary-nav">
        {WORKSPACE_NAV_ITEMS.map((item) => (
          <Link
            key={item.href}
            href={item.href}
            aria-current={pathname === item.href ? "page" : undefined}
            onClick={onNavigate}
          >
            <Icon name={item.icon} /> {item.label}
            {item.activeMarker && (
              <>
                {" "}
                <span className="nav-active-marker" />
              </>
            )}
          </Link>
        ))}
      </nav>
      <div className="nav-label">المنصة</div>
      <nav aria-label="أدوات المنصة" className="primary-nav console-nav">
        {CONSOLE_NAV_ITEMS.map((item) => (
          <Link
            key={item.href}
            href={item.href}
            aria-current={pathname === item.href ? "page" : undefined}
            onClick={onNavigate}
          >
            <Icon name={item.icon} /> {item.label}
          </Link>
        ))}
      </nav>
      <div className="sidebar-note">
        <span className="small-icon">
          <Icon name="layers" />
        </span>
        <h2>من فكرة إلى نتيجة واضحة</h2>
        <p>
          طلبك، خطوات التنفيذ، والأدلة في مساحة واحدة. دون وعود تتجاوز ما نُفّذ.
        </p>
        <Link href="/documents" onClick={onNavigate}>
          جرّب معالجة ملف TXT <Icon name="arrow" width="15" />
        </Link>
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
  const [locale, setLocale] = useState<"ar" | "en">("ar");
  const dialog = useRef<HTMLDialogElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    try {
      const saved = window.localStorage.getItem("cela:locale");
      if (saved === "en" || saved === "ar") {
        setLocale(saved);
        document.documentElement.dir = saved === "ar" ? "rtl" : "ltr";
        document.documentElement.lang = saved;
      }
    } catch {
      /* localStorage optional */
    }
    const resize = () => {
      if (window.innerWidth > 760) dialog.current?.close();
    };
    window.addEventListener("resize", resize);
    return () => window.removeEventListener("resize", resize);
  }, []);
  const toggleLocale = () => {
    const next = locale === "ar" ? "en" : "ar";
    setLocale(next);
    if (typeof document !== "undefined") {
      document.documentElement.dir = next === "ar" ? "rtl" : "ltr";
      document.documentElement.lang = next;
    }
    try {
      window.localStorage.setItem("cela:locale", next);
    } catch {
      /* localStorage optional */
    }
  };
  const title = resolvePageTitle(pathname);
  return (
    <div className="app-frame">
      <AccessGate />
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
          <div className="heading-actions">
            <button
              type="button"
              className="secondary-button locale-toggle"
              onClick={toggleLocale}
              aria-label="تبديل اتجاه ولغة الواجهة"
              dir="ltr"
            >
              {locale === "ar" ? "EN / LTR" : "عربي / RTL"}
            </button>
            <span className="topbar-note">
              <span className="static-dot" />
              بيئة تطوير محلية
            </span>
          </div>
        </header>
        <div id="main-content" tabIndex={-1} className="app-content">
          {children}
        </div>
      </div>
    </div>
  );
}
