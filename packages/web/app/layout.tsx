import type { ReactNode } from "react";
import "./globals.css";

export const metadata = { title: "cela_use_browser — لوحة الوكيل", description: "Live agent console" };

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="ar" dir="rtl">
      <body>{children}</body>
    </html>
  );
}
