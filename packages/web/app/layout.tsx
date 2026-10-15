import type { ReactNode } from "react";
import "./globals.css";
import { AppShell } from "../components/app-shell";

export const metadata = {
  title: "CELIA — مساحة العمل",
  description: "مساحة عمل عربية للمهام والخطوات والأدلة",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="ar" dir="rtl">
      <body>
        <AppShell>{children}</AppShell>
      </body>
    </html>
  );
}
