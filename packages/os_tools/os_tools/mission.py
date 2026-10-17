"""Celia.sh — Mission OS Phase 1 tools: URL inspection, static project inspection,
and SHA-256-signed multi-format deployment & verification reports.

Every tool in this module enforces the platform's core honesty and safety rules:

* **No unverified claims:** static file analysis is explicitly labelled
  ``STATIC_INSPECTION`` with ``tests_executed=False``; it never claims a project's
  test suite passed unless an execution tool actually ran it.
* **SSRF & Egress protection:** ``inspect_public_url`` authorizes the initial target
  and every redirect hop through :class:`~os_tools.egress.EgressPolicy` and
  :class:`~os_tools.ssrf.SSRFGuard`.
* **Root confinement & secret hygiene:** ``inspect_project_files`` is confined to
  its workspace root via :func:`~os_tools.files.resolve_under`, skips sensitive
  credential files, and never echoes a detected secret value.
* **Credential-scrubbed, SHA-256-signed reports:** ``generate_deployment_plan_report``
  passes every input and output through :func:`~os_core.redact.scrub_credentials`
  and attaches ``mission-report.md``, ``mission-report.json``, and
  ``mission-report.html`` as downloadable :class:`~os_core.types.Artifact` records
  with verified ``sha256`` and ``size_bytes`` metadata.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from os_core.redact import contains_secret, scrub_credentials, truncate
from os_core.types import Artifact, RiskLevel, ToolResult, ToolStatus

from os_tools.egress import EgressPolicy, EgressRule
from os_tools.files import _SKIPPED_DIRS, _is_sensitive, os_walk_sorted, resolve_under
from os_tools.registry import Tool
from os_tools.ssrf import SSRFGuard

_REDIRECT_CODES = {301, 302, 303, 307, 308}
_TITLE_RE = re.compile(r"(?is)<title\b[^>]*>(.*?)</title\s*>")
_TAG_RE = re.compile(r"(?s)<[^>]+>")
_WS_RE = re.compile(r"\s+")

SECURITY_HEADER_NAMES: tuple[str, ...] = (
    "strict-transport-security",
    "content-security-policy",
    "x-frame-options",
    "x-content-type-options",
    "referrer-policy",
)

KNOWN_CONFIG_FILES: frozenset[str] = frozenset(
    {
        "pyproject.toml",
        "requirements.txt",
        "setup.py",
        "package.json",
        "package-lock.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "Cargo.toml",
        "Dockerfile",
        "docker-compose.yml",
        "docker-compose.yaml",
        "render.yaml",
        "wrangler.toml",
        "netlify.toml",
        "vercel.json",
        ".env.example",
        ".env.sample",
        ".env.template",
    }
)


def _canonical_sha256(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _split_hosts(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    return tuple(h.strip().lower() for h in raw.split(",") if h.strip())


def default_mission_url_policy(*, resolve_dns: bool = False) -> EgressPolicy:
    """Public http(s) on 80/443 above the non-bypassable SSRF floor."""
    allow_hosts = _split_hosts(os.environ.get("CELA_MISSION_ALLOW_HOSTS")) or ("*",)
    deny_hosts = _split_hosts(os.environ.get("CELA_MISSION_DENY_HOSTS"))
    rules: list[EgressRule] = [
        EgressRule(
            action="deny",
            host=host,
            schemes={"http", "https"},
            ports={80, 443},
            tools={"inspect_public_url"},
        )
        for host in deny_hosts
    ]
    rules.extend(
        EgressRule(
            action="allow",
            host=host,
            schemes={"http", "https"},
            ports={80, 443},
            tools={"inspect_public_url"},
        )
        for host in allow_hosts
    )
    return EgressPolicy(rules=rules, guard=SSRFGuard(resolve_dns=resolve_dns))


def _extract_title(raw_html: str) -> str:
    match = _TITLE_RE.search(raw_html)
    if not match:
        return ""
    cleaned = _WS_RE.sub(" ", _TAG_RE.sub(" ", match.group(1))).strip()
    return truncate(scrub_credentials(html.unescape(cleaned)), 200, note="…")


async def inspect_public_url(
    url: str,
    *,
    timeout: float = 15.0,  # noqa: ASYNC109
    policy: EgressPolicy | None = None,
    transport: Any = None,
    max_bytes: int = 131_072,
    max_redirects: int = 3,
) -> dict[str, Any]:
    """Inspect a public HTTP(S) URL's reachability and security headers safely."""
    import httpx

    active_policy = policy if policy is not None else default_mission_url_policy()
    bounded_timeout = max(1.0, min(float(timeout), 30.0))
    headers = {
        "User-Agent": (
            "celia-mission-os/0.1 (+https://github.com/sayedelazameydesign-crypto/open_source-)"
        ),
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
    }

    started = time.monotonic()
    current = url
    try:
        async with httpx.AsyncClient(
            timeout=bounded_timeout,
            transport=transport,
            follow_redirects=False,
        ) as client:
            for _ in range(max_redirects + 1):
                active_policy.enforce(current, tool="inspect_public_url")
                async with client.stream("GET", current, headers=headers) as resp:
                    if resp.status_code in _REDIRECT_CODES:
                        nxt = resp.next_request
                        if nxt is None:
                            raise RuntimeError(f"redirect without Location from {current}")
                        current = str(nxt.url)
                        continue

                    chunks: list[bytes] = []
                    size = 0
                    async for chunk in resp.aiter_bytes():
                        chunks.append(chunk)
                        size += len(chunk)
                        if size > max_bytes:
                            break
                    raw = b"".join(chunks)[:max_bytes]
                    encoding = resp.encoding or "utf-8"
                    body_text = raw.decode(encoding, errors="replace")

                    lower_headers = {k.lower(): v for k, v in resp.headers.items()}
                    sec_headers = {
                        name: scrub_credentials(lower_headers[name])
                        if name in lower_headers
                        else None
                        for name in SECURITY_HEADER_NAMES
                    }
                    missing = [name for name, val in sec_headers.items() if val is None]
                    elapsed_ms = round((time.monotonic() - started) * 1000, 2)
                    checked_at = datetime.now(UTC).isoformat()

                    core_evidence = {
                        "url": scrub_credentials(url),
                        "final_url": scrub_credentials(str(resp.url)),
                        "status_code": int(resp.status_code),
                        "ok": 200 <= resp.status_code < 400,
                        "content_type": scrub_credentials(lower_headers.get("content-type", "")),
                        "bytes_read": len(raw),
                        "truncated": size > max_bytes,
                        "title": _extract_title(body_text),
                        "security_headers": sec_headers,
                        "missing_security_headers": missing,
                        "verification_kind": "LIVE_HTTP_CHECK",
                        "tests_executed": False,
                    }
                    return {
                        "tool": "inspect_public_url",
                        **core_evidence,
                        "elapsed_ms": elapsed_ms,
                        "checked_at": checked_at,
                        "verification_note_ar": (
                            "تم فحص استجابة الرابط وترويسات الأمان فعليًا عبر HTTP المحمي "
                            "بحارس SSRF؛ هذا الفحص لا يمثل اختبار اختراق شاملًا."
                        ),
                        "evidence_sha256": _canonical_sha256(core_evidence),
                    }
            raise RuntimeError(f"too many redirects (>{max_redirects}) starting at {url}")
    except Exception as exc:
        raise RuntimeError(scrub_credentials(f"{type(exc).__name__}: {exc}")) from None


def inspect_project_files(
    path: str = ".",
    *,
    root: str | Path = ".",
    max_files: int = 200,
    max_file_bytes: int = 131_072,
) -> dict[str, Any]:
    """Perform a bounded, read-only static inspection of a project directory."""
    workspace = Path(root).resolve()
    target = resolve_under(workspace, path)
    if not target.exists():
        raise ValueError(f"path does not exist: {path}")
    if target.is_file():
        target = target.parent

    bounded_max = max(1, min(int(max_files), 500))
    inspected_files: list[str] = []
    config_files: list[str] = []
    ci_workflows: list[str] = []
    secret_hits: list[dict[str, Any]] = []
    stacks: set[str] = set()

    has_package_json = False
    has_node_lockfile = False
    has_env_template = False
    has_pyproject_or_req = False

    for current_dir, dirs, files in os_walk_sorted(target):
        dirs[:] = [
            d
            for d in dirs
            if d not in _SKIPPED_DIRS and not d.startswith(".venv") and d != ".cache"
        ]
        for fname in files:
            if len(inspected_files) >= bounded_max:
                break
            fpath = Path(current_dir) / fname
            if _is_sensitive(fpath):
                continue
            rel = str(fpath.relative_to(target)).replace("\\", "/")
            inspected_files.append(rel)

            if fname in KNOWN_CONFIG_FILES:
                config_files.append(rel)
            if fname in {".env.example", ".env.sample", ".env.template"}:
                has_env_template = True
            if fname in {"pyproject.toml", "requirements.txt", "setup.py"}:
                stacks.add("python")
                has_pyproject_or_req = True
            if fname == "package.json":
                stacks.add("nodejs")
                has_package_json = True
            if fname in {"package-lock.json", "pnpm-lock.yaml", "yarn.lock"}:
                has_node_lockfile = True
            if fname in {"next.config.ts", "next.config.js", "next.config.mjs"}:
                stacks.add("nextjs")
            if fname == "Cargo.toml":
                stacks.add("rust")
            if fname in {"Dockerfile", "docker-compose.yml", "docker-compose.yaml"}:
                stacks.add("docker")
            if fname == "render.yaml":
                stacks.add("render")
            if rel.startswith(".github/workflows/") and fname.endswith((".yml", ".yaml")):
                ci_workflows.append(rel)
                stacks.add("github_actions")

            # Scan small text files for accidental hardcoded secrets and framework hints.
            try:
                raw = fpath.read_bytes()
            except OSError:
                continue
            if b"\x00" in raw or len(raw) > max_file_bytes:
                continue
            text = raw.decode("utf-8", errors="replace")
            if fname in {"pyproject.toml", "requirements.txt"} and "fastapi" in text.lower():
                stacks.add("fastapi")
            if not fname.startswith("test_") and "/tests/" not in f"/{rel}":
                for idx, line in enumerate(text.splitlines(), start=1):
                    if contains_secret(line):
                        secret_hits.append({"file": rel, "line": idx})
                        break
        if len(inspected_files) >= bounded_max:
            break

    findings: list[dict[str, str]] = []
    if secret_hits:
        locations = ", ".join(f"{h['file']}:{h['line']}" for h in secret_hits[:5])
        findings.append(
            {
                "id": "hardcoded_secret_pattern",
                "severity": "high",
                "category": "security",
                "title_ar": "رصد نمط مفتاح سري مضمّن داخل الملفات",
                "detail_ar": (
                    f"تم رصد نمط يشبه مفتاح API أو سرًا في ({locations}) دون طباعة قيمته. "
                    "انقل الأسرار إلى متغيرات البيئة واحذفها من الكود المصدري."
                ),
                "evidence_path": secret_hits[0]["file"],
            }
        )
    if not ci_workflows:
        findings.append(
            {
                "id": "missing_ci_workflow",
                "severity": "medium",
                "category": "ci_cd",
                "title_ar": "غياب مسارات الفحص التلقائي في GitHub Actions",
                "detail_ar": (
                    "لم يُعثر على ملفات `.github/workflows/*.yml` لتشغيل الاختبارات "
                    "وفحوص الأمان تلقائيًا."
                ),
                "evidence_path": ".github/workflows",
            }
        )
    if has_package_json and not has_node_lockfile:
        findings.append(
            {
                "id": "missing_node_lockfile",
                "severity": "medium",
                "category": "dependencies",
                "title_ar": "غياب ملف قفل التبعيات للواجهة (Lockfile)",
                "detail_ar": (
                    "يوجد `package.json` دون `package-lock.json` أو `pnpm-lock.yaml`، "
                    "مما يمنع البناء الحتمي عبر `npm ci`."
                ),
                "evidence_path": "package.json",
            }
        )
    if (has_pyproject_or_req or has_package_json) and not has_env_template:
        findings.append(
            {
                "id": "missing_env_template",
                "severity": "low",
                "category": "configuration",
                "title_ar": "غياب قالب متغيرات البيئة (`.env.example`)",
                "detail_ar": (
                    "يُنصح بإضافة `.env.example` بأسماء المتغيرات المطلوبة فارغة لتوثيق "
                    "عقد التشغيل الآمن."
                ),
                "evidence_path": ".env.example",
            }
        )

    core_evidence = {
        "inspected_root": str(target.relative_to(workspace)) if target != workspace else ".",
        "files_scanned_count": len(inspected_files),
        "stacks_detected": sorted(stacks),
        "config_files_found": sorted(config_files),
        "ci_workflows_found": sorted(ci_workflows),
        "secret_pattern_locations": secret_hits,
        "findings": findings,
        "verification_kind": "STATIC_INSPECTION",
        "tests_executed": False,
    }
    return {
        "tool": "inspect_project_files",
        **core_evidence,
        "checked_at": datetime.now(UTC).isoformat(),
        "verification_note_ar": (
            "فحص ساكن لبنية المشروع وملفات الإعداد فقط — لم يتم تشغيل اختبارات المشروع "
            "تنفيذيًا في هذه الأداة."
        ),
        "evidence_sha256": _canonical_sha256(core_evidence),
    }


def _parse_json_or_none(raw: str | dict[str, Any] | None) -> dict[str, Any] | None:
    if raw is None:
        return None
    if isinstance(raw, dict):
        return json.loads(scrub_credentials(json.dumps(raw, ensure_ascii=False, default=str)))
    if not raw or not raw.strip():
        return None
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        return None


def _build_html_report(report_data: dict[str, Any]) -> str:
    esc = html.escape
    project = esc(report_data["project_name"])
    goal = esc(report_data["goal"])
    generated_at = esc(report_data["generated_at"])
    ver_note = esc(report_data["verification"]["summary_ar"])
    tests_status = esc(report_data["verification"]["tests_execution_status_ar"])

    findings_rows = "".join(
        f"<tr><td>{esc(f.get('severity', 'info'))}</td>"
        f"<td>{esc(f.get('title_ar', ''))}</td>"
        f"<td>{esc(f.get('detail_ar', ''))}</td>"
        f"<td><code>{esc(f.get('evidence_path', '-'))}</code></td></tr>"
        for f in report_data["findings"]
    ) or "<tr><td colspan='4'>لم تُرصد مخالفات حرجة في الفحص الساكن المحدود.</td></tr>"

    stack_rows = "".join(
        f"<tr><td><strong>{esc(item['layer'])}</strong></td>"
        f"<td>{esc(item['service'])}</td>"
        f"<td>{esc(item['role_ar'])}</td>"
        f"<td>{esc(item['free_tier_limit_ar'])}</td></tr>"
        for item in report_data["deployment_plan"]["recommended_stack"]
    )

    verified_list = "".join(f"<li>{esc(v)}</li>" for v in report_data["verified_items"])
    unverified_list = "".join(f"<li>{esc(u)}</li>" for u in report_data["unverified_items"])

    return (
        "<!DOCTYPE html>\n"
        '<html lang="ar" dir="rtl">\n'
        "<head>\n"
        '  <meta charset="utf-8">\n'
        '  <meta name="viewport" content="width=device-width, initial-scale=1">\n'
        '  <meta http-equiv="Content-Security-Policy" '
        "content=\"default-src 'none'; style-src 'unsafe-inline'\">\n"
        f"  <title>تقرير مهمة Celia.sh — {project}</title>\n"
        "  <style>\n"
        "    body { font-family: system-ui, -apple-system, sans-serif; background: #0b0f19; "
        "color: #f1f5f9; margin: 0; padding: 24px; line-height: 1.7; }\n"
        "    .container { max-width: 960px; margin: 0 auto; background: #111827; "
        "border: 1px solid #1f2937; border-radius: 12px; padding: 28px; }\n"
        "    h1, h2 { color: #38bdf8; margin-top: 0; }\n"
        "    .badge { display: inline-block; padding: 4px 10px; border-radius: 6px; "
        "background: #1e293b; color: #fbbf24; font-size: 0.9rem; }\n"
        "    table { width: 100%; border-collapse: collapse; margin: 16px 0; }\n"
        "    th, td { border: 1px solid #334155; padding: 10px 12px; text-align: right; "
        "vertical-align: top; }\n"
        "    th { background: #1e293b; color: #e2e8f0; }\n"
        "    code { background: #0f172a; color: #93c5fd; padding: 2px 6px; border-radius: 4px; "
        "direction: ltr; display: inline-block; }\n"
        "    .box { background: #0f172a; border: 1px solid #334155; border-radius: 8px; "
        "padding: 16px; margin-bottom: 18px; }\n"
        "  </style>\n"
        "</head>\n"
        "<body>\n"
        '  <div class="container">\n'
        f"    <h1>تقرير مهمة Celia.sh — Mission OS: {project}</h1>\n"
        f'    <p><span class="badge">{ver_note}</span> · وقت التوليد: '
        f"<code>{generated_at}</code></p>\n"
        '    <div class="box">\n'
        "      <h2>1. فهم المهمة والهدف</h2>\n"
        f"      <p><strong>الهدف المطلوب:</strong> {goal}</p>\n"
        f"      <p><strong>حالة اختبارات التشغيل:</strong> {tests_status}</p>\n"
        "    </div>\n"
        "    <h2>2. نتائج الفحص والأدلة المسجلة</h2>\n"
        "    <table>\n"
        "      <thead><tr><th>الخطورة</th><th>النتيجة</th>"
        "<th>التفصيل والإصلاح المقترح</th><th>الدليل</th></tr></thead>\n"
        f"      <tbody>{findings_rows}</tbody>\n"
        "    </table>\n"
        "    <h2>3. خطة النشر المجانية والآمنة (0$ دون بطاقة بنكية)</h2>\n"
        "    <table>\n"
        "      <thead><tr><th>الطبقة</th><th>المنصة المرشحة</th>"
        "<th>الدور</th><th>قيد الخطة المجانية والاحتياط الهندسي</th></tr></thead>\n"
        f"      <tbody>{stack_rows}</tbody>\n"
        "    </table>\n"
        '    <div class="box">\n'
        "      <h2>4. ما تم التحقق منه فعليًا مقابل ما لم يتم التحقق منه</h2>\n"
        "      <h3>أ. ما تم إثباته بأداة فعلية (VERIFIED):</h3>\n"
        f"      <ul>{verified_list}</ul>\n"
        "      <h3>ب. ما لم يتم التحقق منه (NOT VERIFIED):</h3>\n"
        f"      <ul>{unverified_list}</ul>\n"
        "    </div>\n"
        "  </div>\n"
        "</body>\n"
        "</html>\n"
    )


def _build_markdown_report(report_data: dict[str, Any]) -> str:
    ver_kind = report_data["verification"]["kind"]
    ver_summary = report_data["verification"]["summary_ar"]
    lines: list[str] = [
        f"# تقرير مهمة Celia.sh — Mission OS: {report_data['project_name']}",
        "",
        f"- **وقت التوليد (UTC):** `{report_data['generated_at']}`",
        f"- **تصنيف التحقق:** `{ver_kind}` — {ver_summary}",
        f"- **حالة اختبارات التشغيل:** {report_data['verification']['tests_execution_status_ar']}",
        "",
        "## 1. فهم المهمة والهدف",
        "",
        f"**الهدف:** {report_data['goal']}",
        "",
        f"**التقنيات المرصودة:** {', '.join(report_data['stacks_detected']) or 'غير محددة'}",
        "",
        "## 2. نتائج الفحص ونقاط الضعف المرصودة",
        "",
    ]
    if report_data["findings"]:
        lines.append("| الخطورة | النتيجة | التفصيل والإصلاح | الدليل |")
        lines.append("|---|---|---|---|")
        for f in report_data["findings"]:
            lines.append(
                f"| `{f.get('severity', 'info')}` | {f.get('title_ar', '')} | "
                f"{f.get('detail_ar', '')} | `{f.get('evidence_path', '-')}` |"
            )
    else:
        lines.append("- لم تُرصد مخالفات حرجة ضمن نطاق الفحص الساكن المحدود.")

    lines.extend(
        [
            "",
            "## 3. خطة النشر المجانية والآمنة (0$ دون بطاقة بنكية)",
            "",
            "| الطبقة | الخدمة | الدور | قيد الخطة المجانية والاحتياط |",
            "|---|---|---|---|",
        ]
    )
    for item in report_data["deployment_plan"]["recommended_stack"]:
        lines.append(
            f"| **{item['layer']}** | {item['service']} | {item['role_ar']} | "
            f"{item['free_tier_limit_ar']} |"
        )

    lines.extend(
        [
            "",
            "## 4. سجل الثقة: ما تم إثباته مقابل ما لم يتم التحقق منه",
            "",
            "### أ. ما تم التحقق منه فعليًا بأداة (VERIFIED):",
            *[f"- {v}" for v in report_data["verified_items"]],
            "",
            "### ب. ما لم يتم التحقق منه (NOT VERIFIED):",
            *[f"- {u}" for u in report_data["unverified_items"]],
            "",
        ]
    )
    return "\n".join(lines)


def generate_deployment_plan_report(
    project_name: str,
    goal: str,
    inspection_summary: str | dict[str, Any] = "",
    url_check_summary: str | dict[str, Any] = "",
    executed_tests_output: str = "",
) -> ToolResult:
    """Generate a scrubbed, SHA-256-signed Mission OS report in MD, JSON, and HTML."""
    clean_project = scrub_credentials(project_name.strip() or "cela-project")
    clean_goal = scrub_credentials(goal.strip() or "فحص المشروع وبناء خطة نشر مجانية وآمنة")
    clean_inspection: str | dict[str, Any] = (
        inspection_summary
        if isinstance(inspection_summary, dict)
        else scrub_credentials(str(inspection_summary))
    )
    clean_url_check: str | dict[str, Any] = (
        url_check_summary
        if isinstance(url_check_summary, dict)
        else scrub_credentials(str(url_check_summary))
    )
    clean_tests = scrub_credentials(executed_tests_output.strip())

    parsed_inspection = _parse_json_or_none(clean_inspection) or {}
    parsed_url = _parse_json_or_none(clean_url_check) or {}

    stacks = list(parsed_inspection.get("stacks_detected") or ["python", "fastapi", "nextjs"])
    findings = list(parsed_inspection.get("findings") or [])
    if parsed_url.get("missing_security_headers"):
        missing_hdrs = ", ".join(parsed_url["missing_security_headers"])
        findings.append(
            {
                "id": "missing_http_security_headers",
                "severity": "medium",
                "category": "web_security",
                "title_ar": "نقص في ترويسات الأمان للرابط العام",
                "detail_ar": f"الترويسات غير المرصودة في الاستجابة: {missing_hdrs}.",
                "evidence_path": str(parsed_url.get("final_url") or parsed_url.get("url") or "url"),
            }
        )

    tests_ran = bool(clean_tests)
    verified_items: list[str] = []
    unverified_items: list[str] = []

    if parsed_inspection.get("evidence_sha256"):
        verified_items.append(
            f"فحص ساكن لبنية المشروع ({parsed_inspection.get('files_scanned_count', 0)} ملف) "
            f"ببصمة دليل sha256:{parsed_inspection['evidence_sha256'][:16]}…"
        )
    else:
        verified_items.append(
            "توليد خطة النشر الحتمية وتنقيح المخرجات من الأسرار (scrub_credentials)."
        )

    if parsed_url.get("evidence_sha256"):
        url_sha = parsed_url["evidence_sha256"][:16]
        verified_items.append(
            f"فحص استجابة HTTP للرابط ({parsed_url.get('final_url')}) بكود حالة "
            f"{parsed_url.get('status_code')} وبصمة دليل sha256:{url_sha}…"
        )
    else:
        unverified_items.append(
            "لم يُفحص رابط عام حي في هذه المهمة (أو لم يُمرَّر ملخص فحص الرابط)."
        )

    if tests_ran:
        verified_items.append(f"تم تمرير مخرج تشغيل اختبارات فعلي: {truncate(clean_tests, 160)}")
    else:
        unverified_items.append(
            "لم يتم تشغيل اختبارات المشروع تنفيذيًا (الفحص الساكن للملفات لا يثبت نجاح الاختبارات)."
        )

    unverified_items.append(
        "أهلية الحساب الفردي في الخطط المجانية واستمرارية توافر الخدمات الخارجية على "
        "مدار الساعة خارج ضمان الأداة."
    )

    recommended_stack = [
        {
            "layer": "الواجهة الأمامية (Frontend + BFF)",
            "service": "Cloudflare Pages + Workers",
            "role_ar": "نشر الواجهة العربية (RTL) وإخفاء رموز الـ API داخل Worker خادمي",
            "free_tier_limit_ar": (
                "500 عملية بناء شهريًا و100,000 طلب Worker يوميًا دون بطاقة؛ "
                "لا تضع الرموز في المتصفح"
            ),
        },
        {
            "layer": "الخادم الخلفي (FastAPI + Agent Core)",
            "service": "Render Free Web Service",
            "role_ar": "تشغيل خادم FastAPI والتخطيط وحواجز SSRF وHITL",
            "free_tier_limit_ar": (
                "ينام بعد 15 دقيقة بلا نشاط وملفاته المحلية مؤقتة؛ لا تحفظ البيانات "
                "الدائمة على القرص المحلي"
            ),
        },
        {
            "layer": "قاعدة البيانات الدائمة (RunStore)",
            "service": "Supabase Postgres Free",
            "role_ar": (
                "حفظ حالة المهام والأحداث والتقارير عبر postgresql:// خارج قرص Render المؤقت"
            ),
            "free_tier_limit_ar": (
                "500 ميجابايت، وقد يتوقف المشروع بعد 7 أيام من انعدام النشاط؛ "
                "يلزم نسخ احتياطي دوري"
            ),
        },
        {
            "layer": "التكامل المستمر والفحص (CI/CD)",
            "service": "GitHub Actions",
            "role_ar": "تشغيل الاختبارات وفحص الأمان والنشر الآلي عند كل تحديث",
            "free_tier_limit_ar": (
                "مجاني للمستودعات العامة و2,000 دقيقة شهريًا للحساب الشخصي في المستودعات الخاصة"
            ),
        },
        {
            "layer": "الأتمتة والويب هوكس الاختيارية",
            "service": "Activepieces",
            "role_ar": "ربط الإشعارات والويب هوكس الخارجية دون إدارة خادم إضافي",
            "free_tier_limit_ar": (
                "حصة مجانية محدودة (100 رصيد يوميًا / 1,000 شهريًا) تتوقف عند تجاوز السقف"
            ),
        },
    ]

    generated_at = datetime.now(UTC).isoformat()
    report_data: dict[str, Any] = {
        "schema_version": "celia.mission.report.v1",
        "project_name": clean_project,
        "goal": clean_goal,
        "generated_at": generated_at,
        "stacks_detected": stacks,
        "verification": {
            "kind": " DETERMINISTIC_REPORT_WITH_EVIDENCE".strip(),
            "static_inspection_only": not tests_ran,
            "tests_executed": tests_ran,
            "live_url_checked": bool(parsed_url.get("evidence_sha256")),
            "goal_verification": "not_verified",
            "summary_ar": (
                "تقرير مبني على أدلة مسجلة وفحص ساكن — لا يدعي نجاح اختبارات لم تُشغَّل"
                if not tests_ran
                else "تقرير مبني على أدلة مسجلة مع مخرج اختبار مرفق"
            ),
            "tests_execution_status_ar": (
                "لم تُشغَّل اختبارات المشروع تنفيذيًا (فحص ساكن فقط)"
                if not tests_ran
                else f"شُغِّلت وأُرفق الدليل: {truncate(clean_tests, 120)}"
            ),
        },
        "findings": findings,
        "deployment_plan": {
            "target_monthly_cost_usd": 0,
            "requires_credit_card": False,
            "recommended_stack": recommended_stack,
        },
        "verified_items": verified_items,
        "unverified_items": unverified_items,
        "evidence_digests": {
            "project_inspection_sha256": parsed_inspection.get("evidence_sha256"),
            "url_inspection_sha256": parsed_url.get("evidence_sha256"),
        },
    }

    md_text = scrub_credentials(_build_markdown_report(report_data))
    json_text = scrub_credentials(json.dumps(report_data, ensure_ascii=False, indent=2))
    html_text = scrub_credentials(_build_html_report(report_data))

    artifacts: list[Artifact] = []
    artifact_manifests: list[dict[str, Any]] = []
    for filename, kind, mime_type, content in (
        ("mission-report.md", "markdown", "text/markdown; charset=utf-8", md_text),
        ("mission-report.json", "json", "application/json; charset=utf-8", json_text),
        ("mission-report.html", "html", "text/html; charset=utf-8", html_text),
    ):
        raw_bytes = content.encode("utf-8")
        digest = hashlib.sha256(raw_bytes).hexdigest()
        meta = {
            "sha256": digest,
            "size_bytes": len(raw_bytes),
            "verification_kind": report_data["verification"]["kind"],
            "tests_executed": tests_ran,
        }
        artifacts.append(
            Artifact(
                kind=kind,  # type: ignore[arg-type]
                filename=filename,
                content=content,
                mime_type=mime_type,
                metadata=meta,
            )
        )
        artifact_manifests.append(
            {
                "filename": filename,
                "kind": kind,
                "mime_type": mime_type,
                "size_bytes": len(raw_bytes),
                "sha256": digest,
            }
        )

    output_summary = {
        "tool": "generate_deployment_plan_report",
        "project_name": clean_project,
        "generated_at": generated_at,
        "verification": report_data["verification"],
        "findings_count": len(findings),
        "artifacts": artifact_manifests,
    }
    return ToolResult(
        tool_call_id="call_report",
        tool_name="generate_deployment_plan_report",
        status=ToolStatus.SUCCESS,
        output=json.dumps(output_summary, ensure_ascii=False, indent=2),
        artifacts=artifacts,
    )


def inspect_public_url_tool(
    *,
    policy: EgressPolicy | None = None,
    transport: Any = None,
    max_bytes: int = 131_072,
    timeout: float = 15.0,
) -> Tool:
    """Build the ``inspect_public_url`` tool for :class:`~os_tools.registry.ToolRegistry`."""

    async def _run(url: str, timeout: float = timeout) -> dict[str, Any]:  # noqa: ASYNC109
        return await inspect_public_url(
            url,
            timeout=timeout,
            policy=policy,
            transport=transport,
            max_bytes=max_bytes,
        )

    return Tool(
        name="inspect_public_url",
        description=(
            "Inspect a public http(s) URL for availability, status code, title, and HTTP "
            "security headers (HSTS, CSP, X-Frame-Options, X-Content-Type-Options, "
            "Referrer-Policy). Protected by SSRFGuard and EgressPolicy at every redirect hop."
        ),
        parameters={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Absolute public http(s) URL to inspect"},
                "timeout": {
                    "type": "number",
                    "description": "Request timeout in seconds (1..30)",
                    "default": timeout,
                },
            },
            "required": ["url"],
        },
        risk=RiskLevel.NETWORK,
        timeout=timeout + 5.0,
        fn=_run,
    )


def inspect_project_files_tool(
    root: str | Path = ".",
    *,
    max_files: int = 200,
    max_file_bytes: int = 131_072,
) -> Tool:
    """Build the ``inspect_project_files`` tool confined to *root*."""

    def _run(path: str = ".", max_files: int = max_files) -> dict[str, Any]:
        return inspect_project_files(
            path,
            root=root,
            max_files=max_files,
            max_file_bytes=max_file_bytes,
        )

    return Tool(
        name="inspect_project_files",
        description=(
            "Perform a read-only static inspection of project files under the workspace root. "
            "Detects stacks, configuration files, CI workflows, missing lockfiles, and accidental "
            "secret patterns without executing project tests."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative directory path under the workspace root",
                    "default": ".",
                },
                "max_files": {
                    "type": "integer",
                    "description": "Maximum number of files to scan (1..500)",
                    "default": max_files,
                },
            },
        },
        risk=RiskLevel.READ_ONLY,
        timeout=30.0,
        fn=_run,
    )


def generate_deployment_plan_report_tool() -> Tool:
    """Build the ``generate_deployment_plan_report`` tool producing MD, JSON, and HTML artifacts."""
    return Tool(
        name="generate_deployment_plan_report",
        description=(
            "Generate a credential-scrubbed, SHA-256-signed Mission OS deployment and inspection "
            "report in three downloadable formats (mission-report.md, mission-report.json, "
            "mission-report.html), explicitly separating verified tool evidence from "
            "unverified items."
        ),
        parameters={
            "type": "object",
            "properties": {
                "project_name": {"type": "string", "description": "Name of the inspected project"},
                "goal": {"type": "string", "description": "User mission goal in Arabic or English"},
                "inspection_summary": {
                    "type": "string",
                    "description": "JSON or text output from inspect_project_files",
                    "default": "",
                },
                "url_check_summary": {
                    "type": "string",
                    "description": "JSON or text output from inspect_public_url",
                    "default": "",
                },
                "executed_tests_output": {
                    "type": "string",
                    "description": (
                        "Actual test execution output if tests were run; leave empty "
                        "for static inspection"
                    ),
                    "default": "",
                },
            },
            "required": ["project_name", "goal"],
        },
        risk=RiskLevel.READ_ONLY,
        timeout=30.0,
        fn=generate_deployment_plan_report,
    )


def mission_tools(
    *,
    root: str | Path = ".",
    url_policy: EgressPolicy | None = None,
    transport: Any = None,
) -> list[Tool]:
    """Return all three Celia.sh — Mission OS Phase 1 tools."""
    return [
        inspect_public_url_tool(policy=url_policy, transport=transport),
        inspect_project_files_tool(root=root),
        generate_deployment_plan_report_tool(),
    ]


__all__ = [
    "KNOWN_CONFIG_FILES",
    "SECURITY_HEADER_NAMES",
    "default_mission_url_policy",
    "generate_deployment_plan_report",
    "generate_deployment_plan_report_tool",
    "inspect_project_files",
    "inspect_project_files_tool",
    "inspect_public_url",
    "inspect_public_url_tool",
    "mission_tools",
]
