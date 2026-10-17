"""Celia.sh — Mission OS Phase 1 acceptance tests.

Covers all five approved Phase 1 requirements offline and without API keys:
1. The three real tools: ``inspect_public_url``, ``inspect_project_files``,
   and ``generate_deployment_plan_report``.
2. The three downloadable report formats (Markdown, JSON, HTML) with secret
   redaction and SHA-256 integrity verification.
3. Run retrieval and report download across server restarts without breaking
   existing API contracts (``GET /artifacts/{run_id}/{filename}`` stays JSON).
4. Direct ``postgresql://`` and ``postgres://`` support via ``run_store_from_url``
   alongside existing ``sqlite:///`` durability.
5. Honest verification labelling: static inspection never claims tests ran.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from os_core.types import ToolCall
from os_server.app import create_app
from os_server.demo import build_mission_platform, build_registry
from os_server.storage import PostgresRunStore, run_store_from_url
from os_tools.mission import (
    generate_deployment_plan_report,
    inspect_project_files,
    inspect_public_url,
    mission_tools,
)
from os_tools.registry import ToolRegistry

PLATFORM_TOKEN = "mission-platform-token"
PRIVATE_TOKEN = "mission-private-token"


def _mock_url_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == "https://example.org/app":
            return httpx.Response(
                200,
                headers={
                    "content-type": "text/html; charset=utf-8",
                    "strict-transport-security": "max-age=63072000; includeSubDomains",
                    "x-content-type-options": "nosniff",
                },
                content=b"<html><head><title>Celia Demo App</title></head><body>ok</body></html>",
            )
        if url == "https://example.org/redirect-ok":
            return httpx.Response(302, headers={"Location": "https://example.org/app"})
        if url == "https://example.org/redirect-ssrf":
            return httpx.Response(
                302, headers={"Location": "http://169.254.169.254/latest/meta-data/"}
            )
        return httpx.Response(404, content=b"not found")

    return httpx.MockTransport(handler)


# ---------------------------------------------------------------------------
# 1. Tool 1: inspect_public_url (live HTTP check + SSRF & redirect protection)
# ---------------------------------------------------------------------------


async def test_inspect_public_url_extracts_title_headers_and_sha256() -> None:
    result = await inspect_public_url(
        "https://example.org/app",
        transport=_mock_url_transport(),
    )
    assert result["tool"] == "inspect_public_url"
    assert result["status_code"] == 200
    assert result["ok"] is True
    assert result["title"] == "Celia Demo App"
    assert result["verification_kind"] == "LIVE_HTTP_CHECK"
    assert result["tests_executed"] is False
    assert result["security_headers"]["strict-transport-security"] is not None
    assert result["security_headers"]["x-content-type-options"] == "nosniff"
    assert "content-security-policy" in result["missing_security_headers"]
    assert len(result["evidence_sha256"]) == 64


async def test_inspect_public_url_follows_safe_redirect_and_blocks_ssrf_redirect() -> None:
    ok_res = await inspect_public_url(
        "https://example.org/redirect-ok",
        transport=_mock_url_transport(),
    )
    assert ok_res["final_url"] == "https://example.org/app"
    assert ok_res["status_code"] == 200

    with pytest.raises(RuntimeError, match=r"cloud metadata|169\.254\.169\.254"):
        await inspect_public_url(
            "https://example.org/redirect-ssrf",
            transport=_mock_url_transport(),
        )


@pytest.mark.parametrize(
    "blocked_url",
    [
        "http://127.0.0.1:8080/admin",
        "http://localhost/internal",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.1/secret",
        "ftp://example.org/file",
        "http://intranet/path",
    ],
)
async def test_inspect_public_url_blocks_ssrf_and_scrubs_url_credentials(
    blocked_url: str,
) -> None:
    with pytest.raises(RuntimeError):
        await inspect_public_url(blocked_url, transport=_mock_url_transport())

    with pytest.raises(RuntimeError) as exc_info:
        await inspect_public_url(
            "http://admin:supersecretpass123@127.0.0.1:8080/private",
            transport=_mock_url_transport(),
        )
    assert "supersecretpass123" not in str(exc_info.value)
    assert "REDACTED@" in str(exc_info.value)


# ---------------------------------------------------------------------------
# 2. Tool 2: inspect_project_files (static inspection + root confinement)
# ---------------------------------------------------------------------------


def test_inspect_project_files_detects_stacks_findings_and_never_claims_tests_ran(
    tmp_path: Path,
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "demo"\ndependencies = ["fastapi>=0.115"]\n',
        encoding="utf-8",
    )
    (tmp_path / "package.json").write_text('{"name": "web-ui"}\n', encoding="utf-8")
    # Write a file with a fake provider key shape and a sensitive .env file
    fake_key = "sk-proj-" + "A" * 24
    (tmp_path / "config.py").write_text(f'OPENAI_KEY = "{fake_key}"\n', encoding="utf-8")
    (tmp_path / ".env").write_text(f"SECRET_TOKEN={fake_key}\n", encoding="utf-8")

    report = inspect_project_files(".", root=tmp_path)
    assert report["tool"] == "inspect_project_files"
    assert report["verification_kind"] == "STATIC_INSPECTION"
    assert report["tests_executed"] is False
    assert "لم يتم تشغيل اختبارات المشروع" in report["verification_note_ar"]
    assert {"python", "fastapi", "nodejs"} <= set(report["stacks_detected"])

    # .env is skipped completely; config.py secret location is reported without the secret value
    serialized = json.dumps(report, ensure_ascii=False)
    assert fake_key not in serialized
    assert ".env" not in report["config_files_found"]
    finding_ids = {f["id"] for f in report["findings"]}
    assert "hardcoded_secret_pattern" in finding_ids
    assert "missing_ci_workflow" in finding_ids
    assert "missing_node_lockfile" in finding_ids
    assert "missing_env_template" in finding_ids
    assert len(report["evidence_sha256"]) == 64


def test_inspect_project_files_refuses_path_escape(tmp_path: Path) -> None:
    with pytest.raises(PermissionError, match="escapes the workspace root"):
        inspect_project_files("../outside", root=tmp_path)


# ---------------------------------------------------------------------------
# 3. Tool 3: generate_deployment_plan_report (MD, JSON, HTML + SHA-256 + scrub)
# ---------------------------------------------------------------------------


def test_generate_deployment_plan_report_creates_three_scrubbed_signed_artifacts(
    tmp_path: Path,
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "celia"\ndependencies = ["fastapi"]\n', encoding="utf-8"
    )
    inspection = inspect_project_files(".", root=tmp_path)
    leaked_token = "ghp_" + "B" * 28

    result = generate_deployment_plan_report(
        project_name="Celia.sh <script>alert(1)</script>",
        goal=f"افحص مشروعي مع مفتاح {leaked_token} وأنشئ خطة نشر مجانية",
        inspection_summary=json.dumps(inspection, ensure_ascii=False),
    )
    assert result.ok
    assert len(result.artifacts) == 3

    by_name = {art.filename: art for art in result.artifacts}
    assert set(by_name) == {"mission-report.md", "mission-report.json", "mission-report.html"}

    for name, art in by_name.items():
        # Secret must never appear in any report format
        assert leaked_token not in art.content, f"secret leaked in {name}"
        raw = art.content.encode("utf-8")
        assert hashlib.sha256(raw).hexdigest() == art.metadata["sha256"]
        assert len(raw) == art.metadata["size_bytes"]
        assert art.metadata["tests_executed"] is False

    # JSON report structure & honesty checks
    parsed_json = json.loads(by_name["mission-report.json"].content)
    assert parsed_json["schema_version"] == "celia.mission.report.v1"
    assert parsed_json["verification"]["static_inspection_only"] is True
    assert parsed_json["verification"]["tests_executed"] is False
    assert parsed_json["verification"]["goal_verification"] == "not_verified"
    assert parsed_json["deployment_plan"]["target_monthly_cost_usd"] == 0
    assert parsed_json["deployment_plan"]["requires_credit_card"] is False
    assert any("لم يتم تشغيل اختبارات المشروع" in item for item in parsed_json["unverified_items"])

    # HTML report XSS & CSP safety checks
    html_content = by_name["mission-report.html"].content
    assert 'dir="rtl"' in html_content
    assert "Content-Security-Policy" in html_content
    assert "<script>alert(1)</script>" not in html_content
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html_content


# ---------------------------------------------------------------------------
# 4. End-to-End Mission Run, Persistence, Retrieval & Download (SQLite + Postgres)
# ---------------------------------------------------------------------------


def test_mission_run_persistence_retrieval_and_report_download_over_sqlite(
    tmp_path: Path,
) -> None:
    project_dir = tmp_path / "sample_repo"
    project_dir.mkdir()
    (project_dir / "pyproject.toml").write_text(
        '[project]\nname = "sample"\ndependencies = ["fastapi"]\n', encoding="utf-8"
    )
    (project_dir / ".env.example").write_text("CELA_API_TOKEN=\n", encoding="utf-8")

    db_url = f"sqlite:///{tmp_path / 'mission-runs.db'}"
    transport = _mock_url_transport()

    def factory(mode: str, max_steps: int):
        plat = build_mission_platform(
            root=str(project_dir),
            project_name="Celia.sh Demo",
            goal="افحص مشروعي واختبر الرابط وأنشئ خطة نشر مجانية وآمنة",
            url="https://example.org/app",
            transport=transport,
        )
        plat.config.max_steps = max_steps
        return plat

    headers = {"Authorization": f"Bearer {PLATFORM_TOKEN}"}
    private_headers = {"Authorization": f"Bearer {PRIVATE_TOKEN}"}

    app1 = create_app(
        factory,
        api_token=PLATFORM_TOKEN,
        private_api_token=PRIVATE_TOKEN,
        store_url=db_url,
    )
    with TestClient(app1) as client:
        start = client.post(
            "/runs",
            json={
                "task": "افحص مشروعي واختبر الرابط وأنشئ خطة نشر مجانية وآمنة",
                "mode": "flow",
            },
            headers=headers,
        )
        assert start.status_code == 202
        run_id = start.json()["run_id"]

        waited = client.post(
            f"/cela/v1/runs/{run_id}/wait",
            json={"timeout": 10.0},
            headers=private_headers,
        )
        assert waited.status_code == 200
        assert waited.json()["status"] == "completed"

    # Simulate a full server restart over the same durable SQLite store
    app2 = create_app(
        factory,
        api_token=PLATFORM_TOKEN,
        private_api_token=PRIVATE_TOKEN,
        store_url=db_url,
    )
    with TestClient(app2) as client:
        # 1) Run is listed in GET /runs and retrievable via GET /runs/{run_id}
        runs_list = client.get("/runs", headers=headers)
        assert runs_list.status_code == 200
        assert any(r["run_id"] == run_id for r in runs_list.json()["runs"])

        snap = client.get(f"/runs/{run_id}", headers=headers).json()
        assert snap["status"] == "completed"
        assert len(snap["steps"]) == 3
        assert snap["tool_results_summary"]["successful"] == 3
        assert snap["execution"]["goal_verification"] == "not_verified"

        artifacts = {a["filename"]: a for a in snap["artifacts"]}
        assert set(artifacts) == {
            "mission-report.md",
            "mission-report.json",
            "mission-report.html",
        }

        # 2) Existing metadata route GET /artifacts/{run_id}/{filename} stays JSON
        meta_resp = client.get(f"/artifacts/{run_id}/mission-report.md", headers=headers)
        assert meta_resp.status_code == 200
        assert meta_resp.headers["content-type"].startswith("application/json")
        assert meta_resp.json()["filename"] == "mission-report.md"

        # 3) New download routes verify SHA-256 and return attachment bytes
        for fname, expected_mime in (
            ("mission-report.md", "text/markdown"),
            ("mission-report.json", "application/json"),
            ("mission-report.html", "text/html"),
        ):
            dl = client.get(
                f"/runs/{run_id}/artifacts/{fname}/download",
                headers=headers,
            )
            assert dl.status_code == 200
            assert "attachment" in dl.headers["content-disposition"]
            assert dl.headers["x-content-type-options"] == "nosniff"
            assert dl.headers["cache-control"] == "no-store"
            assert dl.headers["content-type"].startswith(expected_mime)
            digest = hashlib.sha256(dl.content).hexdigest()
            assert digest == artifacts[fname]["sha256"]
            assert dl.headers["x-artifact-sha256"] == digest

        # Also works via /cela/v1/runs/{run_id}/artifacts/{filename}/download
        priv_dl = client.get(
            f"/cela/v1/runs/{run_id}/artifacts/mission-report.json/download",
            headers=private_headers,
        )
        assert priv_dl.status_code == 200
        assert (
            hashlib.sha256(priv_dl.content).hexdigest()
            == artifacts["mission-report.json"]["sha256"]
        )

        # Path traversal or unknown artifact returns 404
        assert (
            client.get(
                f"/runs/{run_id}/artifacts/missing-file.md/download",
                headers=headers,
            ).status_code
            == 404
        )


def test_tampered_artifact_in_store_is_rejected_with_409(tmp_path: Path) -> None:
    db_path = tmp_path / "tamper.db"
    db_url = f"sqlite:///{db_path}"

    def factory(mode: str, max_steps: int):
        return build_mission_platform(root=str(tmp_path))

    headers = {"Authorization": f"Bearer {PLATFORM_TOKEN}"}
    private_headers = {"Authorization": f"Bearer {PRIVATE_TOKEN}"}

    app1 = create_app(
        factory,
        api_token=PLATFORM_TOKEN,
        private_api_token=PRIVATE_TOKEN,
        store_url=db_url,
    )
    with TestClient(app1) as client:
        start_resp = client.post(
            "/runs", json={"task": "inspect", "mode": "flow"}, headers=headers
        )
        run_id = start_resp.json()["run_id"]
        client.post(
            f"/cela/v1/runs/{run_id}/wait", json={"timeout": 10.0}, headers=private_headers
        )

    # Corrupt the stored content inside SQLite run_states while leaving sha256 intact
    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT state_json FROM run_states WHERE run_id=?", (run_id,)).fetchone()
    state_dict = json.loads(row[0])
    for art in state_dict["artifacts"]:
        if art["filename"] == "mission-report.md":
            art["content"] = "TAMPERED CONTENT"
    conn.execute(
        "UPDATE run_states SET state_json=? WHERE run_id=?",
        (json.dumps(state_dict), run_id),
    )
    conn.commit()
    conn.close()

    app2 = create_app(
        factory,
        api_token=PLATFORM_TOKEN,
        private_api_token=PRIVATE_TOKEN,
        store_url=db_url,
    )
    with TestClient(app2) as client:
        resp = client.get(
            f"/runs/{run_id}/artifacts/mission-report.md/download",
            headers=headers,
        )
        assert resp.status_code == 409
        assert "integrity check failed" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# 5. Direct PostgreSQL (postgresql:// and postgres://) support & secret safety
# ---------------------------------------------------------------------------


class _SharedFakePgBackend:
    """In-memory SQLite backing multiple PostgresRunStore instances across restarts."""

    def __init__(self) -> None:
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)

    def connect(self) -> _FakePgConnProxy:
        return _FakePgConnProxy(self.conn)


class _FakePgConnProxy:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def execute(self, sql: str, params: tuple = ()):
        return self._conn.execute(sql.replace("%s", "?"), tuple(params))

    def commit(self) -> None:
        self._conn.commit()

    def close(self) -> None:
        pass  # Keep shared in-memory DB alive across simulated restarts


def test_postgresql_direct_url_selection_repr_redaction_and_restart_recovery(
    tmp_path: Path,
) -> None:
    dsn = "postgresql://postgres:SupaSecretPass999@db.example.supabase.co:5432/postgres"
    pg_backend = _SharedFakePgBackend()

    store1 = run_store_from_url(dsn, connection_factory=pg_backend.connect)
    assert isinstance(store1, PostgresRunStore)
    assert "SupaSecretPass999" not in repr(store1)
    assert "REDACTED@" in repr(store1)

    def factory(mode: str, max_steps: int):
        return build_mission_platform(root=str(tmp_path))

    from os_server.runtime import ApiRuntime

    rt1 = ApiRuntime(factory, store=store1)
    app1 = create_app(
        factory,
        api_token=PLATFORM_TOKEN,
        private_api_token=PRIVATE_TOKEN,
        runtime=rt1,
    )
    headers = {"Authorization": f"Bearer {PLATFORM_TOKEN}"}
    private_headers = {"Authorization": f"Bearer {PRIVATE_TOKEN}"}

    with TestClient(app1) as client:
        run_id = client.post(
            "/runs", json={"task": "افحص المشروع", "mode": "flow"}, headers=headers
        ).json()["run_id"]
        waited = client.post(
            f"/cela/v1/runs/{run_id}/wait", json={"timeout": 10.0}, headers=private_headers
        )
        assert waited.status_code == 200
    rt1.close()

    # Second runtime over a fresh PostgresRunStore connected to the same backend
    store2 = run_store_from_url(dsn, connection_factory=pg_backend.connect)
    rt2 = ApiRuntime(factory, store=store2)
    app2 = create_app(
        factory,
        api_token=PLATFORM_TOKEN,
        private_api_token=PRIVATE_TOKEN,
        runtime=rt2,
    )
    with TestClient(app2) as client:
        snap = client.get(f"/runs/{run_id}", headers=headers)
        assert snap.status_code == 200
        assert snap.json()["status"] == "completed"
        dl = client.get(
            f"/runs/{run_id}/artifacts/mission-report.md/download",
            headers=headers,
        )
        assert dl.status_code == 200
        assert "تقرير مهمة Celia.sh" in dl.text
    rt2.close()


async def test_mission_tools_registered_in_default_registry_and_callable(
    tmp_path: Path,
) -> None:
    registry, _guard, _browser = build_registry()
    assert {
        "inspect_public_url",
        "inspect_project_files",
        "generate_deployment_plan_report",
    } <= set(registry.names())

    local_reg = ToolRegistry()
    local_reg.extend(mission_tools(root=tmp_path, transport=_mock_url_transport()))
    res = await local_reg.execute(ToolCall(name="inspect_project_files", args={"path": "."}))
    assert res.ok
    assert json.loads(res.output)["verification_kind"] == "STATIC_INSPECTION"
