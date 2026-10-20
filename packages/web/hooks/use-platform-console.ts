"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import {
  parseLedgerReport,
  parsePlatformInfo,
  type LedgerReport,
  type PlatformInfo,
} from "../lib/platform";

/**
 * Data hooks for the read-only platform console.
 *
 * Both hooks fetch same-origin `/api/backend/*` routes; the Next.js server
 * holds the API token and the browser never sees it. Malformed payloads are
 * rejected by the parsers in `lib/platform.ts`, so a broken backend cannot be
 * rendered as a healthy platform.
 */

export type Resource<T> = {
  data: T | null;
  error: string;
  loading: boolean;
  refreshedAt: number | null;
  refresh: () => void;
};

function failureMessage(status: number | null): string {
  if (status === 401) {
    return "بوابة الوصول رفضت الطلب (401). أدخل إثبات المشغّل ثم أعد المحاولة.";
  }
  if (status === 503) {
    return "بوابة الوصول غير مهيأة على الخادم (503)، أو أن الخلفية ما زالت تُقلع. لم نفترض أي بيانات.";
  }
  if (status === 502) return "تعذر الوصول إلى الخلفية (502). لا تُعرض بيانات قديمة كأنها حيّة.";
  if (status === 404) return "العقد المطلوب غير موجود على هذه الخلفية (404).";
  if (status !== null) return `فشل الطلب بحالة HTTP ${status}. لم نفترض نجاحًا.`;
  return "تعذر إكمال الطلب لأسباب شبكية. لم نفترض نجاحًا.";
}

function useJsonResource<T>(
  url: string,
  parse: (value: unknown) => T,
): Resource<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [refreshedAt, setRefreshedAt] = useState<number | null>(null);
  const active = useRef<AbortController | null>(null);

  const refresh = useCallback(() => {
    active.current?.abort();
    const controller = new AbortController();
    active.current = controller;
    setLoading(true);
    setError("");
    void (async () => {
      let status: number | null = null;
      try {
        const response = await fetch(url, {
          signal: controller.signal,
          headers: { Accept: "application/json" },
          cache: "no-store",
        });
        status = response.status;
        const body: unknown = await response.json().catch(() => null);
        if (!response.ok) throw new Error(failureMessage(status));
        if (controller.signal.aborted) return;
        setData(parse(body));
        setRefreshedAt(Date.now());
      } catch (cause) {
        if (controller.signal.aborted) return;
        const message =
          cause instanceof Error && cause.message.startsWith("invalid_api_response")
            ? "استجابة الخادم لا تطابق العقد المعلن، فلم تُعرض كحالة منصة."
            : cause instanceof Error && cause.message
              ? cause.message
              : failureMessage(status);
        setError(message);
      } finally {
        if (!controller.signal.aborted) setLoading(false);
      }
    })();
  }, [url, parse]);

  useEffect(() => {
    refresh();
    return () => active.current?.abort();
  }, [refresh]);

  return { data, error, loading, refreshedAt, refresh };
}

export function usePlatformInfo(): Resource<PlatformInfo> {
  return useJsonResource("/api/backend/platform/info", parsePlatformInfo);
}

export function useLedgerReport(limit = 20): Resource<LedgerReport> {
  return useJsonResource(
    `/api/backend/ledger?limit=${encodeURIComponent(String(limit))}`,
    parseLedgerReport,
  );
}

export type ProbeResult = {
  ok: boolean;
  status: number | null;
  ms: number;
  at: number;
  bytes: number;
  note: string;
};

/**
 * A measured round trip to the backend through the same BFF route the pages
 * use. This is a connectivity measurement, not a capability test: a 200 means
 * the proxy reached the API, never that a tool ran or a deployment succeeded.
 */
export async function probePlatformInfo(): Promise<ProbeResult> {
  const started = performance.now();
  try {
    const response = await fetch("/api/backend/platform/info", {
      headers: { Accept: "application/json" },
      cache: "no-store",
    });
    const text = await response.text();
    const ms = Math.round((performance.now() - started) * 10) / 10;
    return {
      ok: response.ok,
      status: response.status,
      ms,
      at: Date.now(),
      bytes: new Blob([text]).size,
      note: response.ok
        ? "استجابة HTTP ناجحة من الخلفية عبر وكيل Next.js."
        : failureMessage(response.status),
    };
  } catch {
    return {
      ok: false,
      status: null,
      ms: Math.round((performance.now() - started) * 10) / 10,
      at: Date.now(),
      bytes: 0,
      note: failureMessage(null),
    };
  }
}
