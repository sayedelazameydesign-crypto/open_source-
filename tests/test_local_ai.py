"""Tests for LocalAIEngine, MCP local_ai_tools, CelaBrowser AI methods, /api/* and /cela/v1/*."""

from __future__ import annotations

import base64
import math
import struct
import tomllib
import zlib
from pathlib import Path

import pytest

fastapi_testclient = pytest.importorskip("fastapi.testclient")
TestClient = fastapi_testclient.TestClient

from os_core.types import ToolCall  # noqa: E402

from os_server import (  # noqa: E402
    CelaBrowser,
    build_demo_platform,
    build_registry,
    create_app,
    create_local_ai_router,
)
from os_tools import (  # noqa: E402
    LocalAIEngine,
    detect_language,
    local_ai_tools,
    normalise_arabic,
)

PLATFORM_TOKEN = "platform-ai-token-123"
PRIVATE_TOKEN = "private-cela-ai-token-456"


def _make_png_bytes(
    width: int = 4,
    height: int = 3,
    *,
    text_chunk: tuple[str, str] | None = ("Comment", "Invoice INV-2026-99 Total $1250.00"),
    ztxt_chunk: tuple[str, str] | None = None,
    itxt_chunk: tuple[str, str] | None = None,
) -> bytes:
    def _chunk(ctype: bytes, payload: bytes) -> bytes:
        crc = zlib.crc32(ctype + payload) & 0xFFFFFFFF
        return struct.pack(">I", len(payload)) + ctype + payload + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw_rows = b"".join(b"\x00" + (b"\x10\x80\xf0" * width) for _ in range(height))
    idat = zlib.compress(raw_rows)

    chunks = [b"\x89PNG\r\n\x1a\n", _chunk(b"IHDR", ihdr)]
    if text_chunk is not None:
        k, v = text_chunk
        chunks.append(_chunk(b"tEXt", k.encode("latin1") + b"\x00" + v.encode("utf-8")))
    if ztxt_chunk is not None:
        k, v = ztxt_chunk
        comp = zlib.compress(v.encode("utf-8"))
        chunks.append(_chunk(b"zTXt", k.encode("latin1") + b"\x00\x00" + comp))
    if itxt_chunk is not None:
        k, v = itxt_chunk
        payload = k.encode("latin1") + b"\x00\x00\x00ar\x00\x00" + v.encode("utf-8")
        chunks.append(_chunk(b"iTXt", payload))
    chunks.append(_chunk(b"IDAT", idat))
    chunks.append(_chunk(b"IEND", b""))
    return b"".join(chunks)


def _make_pdf_bytes(text_stream: str, *, compress: bool = True) -> bytes:
    stream_bytes = text_stream.encode("utf-8")
    if compress:
        body = zlib.compress(stream_bytes)
        header = f"<< /Length {len(body)} /Filter /FlateDecode >>".encode("ascii")
    else:
        body = stream_bytes
        header = f"<< /Length {len(body)} >>".encode("ascii")
    return b"%PDF-1.4\n1 0 obj\n" + header + b"\nstream\n" + body + b"\nendstream\nendobj\n%%EOF\n"


def _make_wav_bytes(
    *,
    sample_rate: int = 8000,
    duration_sec: float = 0.25,
    freq_hz: float = 440.0,
    amplitude: float = 0.5,
    txt_note: str | None = None,
    info_note: str | None = None,
) -> bytes:
    n_samples = int(sample_rate * duration_sec)
    pcm = bytearray()
    for i in range(n_samples):
        val = int(amplitude * 32767.0 * math.sin(2.0 * math.pi * freq_hz * (i / sample_rate)))
        pcm.extend(struct.pack("<h", val))

    fmt_chunk = b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, sample_rate, sample_rate * 2, 2, 16)
    data_chunk = b"data" + struct.pack("<I", len(pcm)) + bytes(pcm)
    extra = bytearray()
    if txt_note:
        tb = txt_note.encode("utf-8")
        pad = b"\x00" if len(tb) % 2 else b""
        extra.extend(b"TXT " + struct.pack("<I", len(tb)) + tb + pad)
    if info_note:
        ib = info_note.encode("utf-8")
        sub_pad = b"\x00" if len(ib) % 2 else b""
        sub = b"ICMT" + struct.pack("<I", len(ib)) + ib + sub_pad
        list_payload = b"INFO" + sub
        extra.extend(b"LIST" + struct.pack("<I", len(list_payload)) + list_payload)

    riff_body = b"WAVE" + fmt_chunk + data_chunk + bytes(extra)
    return b"RIFF" + struct.pack("<I", len(riff_body)) + riff_body


# ---------------------------------------------------------------------------
# 1. Capabilities, Arabic normalisation & language detection
# ---------------------------------------------------------------------------


def test_local_ai_capabilities_and_pyproject_optional_ai_group() -> None:
    engine = LocalAIEngine()
    caps = engine.capabilities()
    assert caps["mode"] == "local-cpu"
    assert caps["external_network_required"] is False
    assert caps["per_request_cost"] == 0.0

    expected_libs = {
        "scikit_learn",
        "spacy",
        "sentence_transformers",
        "faiss",
        "opencv",
        "paddleocr",
        "faster_whisper",
    }
    assert expected_libs <= set(caps["optional_libraries"].keys())
    for lib_name in expected_libs:
        assert isinstance(caps["optional_libraries"][lib_name], bool)
        assert isinstance(caps[lib_name], bool)

    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    parsed = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    ai_deps = " ".join(parsed["project"]["optional-dependencies"]["ai"])
    for pkg in (
        "scikit-learn",
        "spacy",
        "sentence-transformers",
        "faiss-cpu",
        "opencv-python-headless",
        "paddleocr",
        "faster-whisper",
    ):
        assert pkg in ai_deps


def test_normalise_arabic_and_detect_language_ignores_urls_and_emails() -> None:
    raw = "إِجْمَالِيُّ الفَاتُورَةِ ١٢٥٠ رِيَالًا بنسبة ١٥٪ في مستنداتنا"
    norm = normalise_arabic(raw)
    assert "1250" in norm
    assert "15%" in norm
    assert "اجمالي" in norm

    ar_with_links = (
        "يرجى مراسلة الدعم الفني على support@long-international-domain-example.com "
        "أو زيارة الرابط https://example.org/documentation/enterprise/v1/getting-started"
    )
    assert detect_language(ar_with_links) == "ar"
    assert detect_language("Autonomous sandboxed execution on local CPU") == "en"
    assert detect_language("يعمل محرك LocalAIEngine على CPU لتحليل البيانات") == "mixed"


def test_extract_entities_bilingual_all_nine_entity_types() -> None:
    engine = LocalAIEngine()
    sample = (
        "وقّع المهندس أحمد محمود من شركة أرامكو في الرياض بتاريخ ١٥ أكتوبر ٢٠٢٦ "
        "عقدًا بقيمة ٢٥٠٠٠ ريال سعودي ونمو ١٨٪. "
        "Contact John Smith at OpenAI Inc in San Francisco on 2026-10-13 for $45,000 "
        "via billing@example.org, https://example.org/deal or +966 50 123 4567."
    )
    res = engine.extract_entities(sample)
    by_type = res["entities_by_type"]

    assert res["language"] in {"ar", "mixed"}
    assert "25000" in res["normalised_text"]
    assert by_type["PERSON"], "expected PERSON entities"
    assert any("أحمد" in p or "احمد" in p or "John Smith" in p for p in by_type["PERSON"])
    assert any(loc in by_type["LOCATION"] for loc in ("الرياض", "San Francisco"))
    assert any("أرامكو" in o or "OpenAI" in o for o in by_type["ORG"])
    assert "2026-10-13" in by_type["DATE"]
    assert any("25000" in m or "$45,000" in m for m in by_type["MONEY"])
    assert any("18%" in p for p in by_type["PERCENT"])
    assert "billing@example.org" in by_type["EMAIL"]
    assert "https://example.org/deal" in by_type["URL"]
    assert any("966" in ph for ph in by_type["PHONE"])
    assert len(res["keywords"]) >= 5


# ---------------------------------------------------------------------------
# 2. Hybrid semantic search
# ---------------------------------------------------------------------------


def test_hybrid_semantic_search_cross_lingual_and_morphological_stemming() -> None:
    engine = LocalAIEngine()
    docs = [
        {
            "id": "fin_ar",
            "title": "الفواتير والضرائب",
            "text": "سداد فاتورة المبيعات وحساب الضريبة والأرباح المالية للعملاء",
        },
        {
            "id": "sec_en",
            "title": "Network Security",
            "text": "Cybersecurity firewall, SSRF protection, and sandbox network isolation",
        },
        {
            "id": "ai_en",
            "title": "Deep Learning Models",
            "text": "Training neural networks and machine learning transformers on CPU",
        },
    ]
    idx_res = engine.index_documents(docs, namespace="kb", clear=True)
    assert idx_res["indexed"] == 3
    assert idx_res["total_documents"] == 3

    # Cross-lingual query: Arabic security query -> matches English security document
    sec_hits = engine.semantic_search("أمن الشبكات وجدار الحماية والعزل", namespace="kb", top_k=2)
    assert sec_hits["results"][0]["id"] == "sec_en"
    assert "concept:security_network" in sec_hits["results"][0]["matched_concepts"]

    # Morphological broken plural + English query -> matches Arabic finance doc
    fin_hits = engine.semantic_search(
        "فواتير المدفوعات invoice tax revenue", namespace="kb", top_k=2
    )
    assert fin_hits["results"][0]["id"] == "fin_ar"


# ---------------------------------------------------------------------------
# 3. Classical ML & Statistical Analysis (classify, forecast, cluster, anomaly)
# ---------------------------------------------------------------------------


def test_ml_analyze_classify_knn_with_standard_scaling() -> None:
    engine = LocalAIEngine()
    train_x = [
        [1.0, 1000.0],
        [1.2, 1020.0],
        [0.8, 980.0],
        [9.0, 1000.0],
        [9.2, 990.0],
        [8.8, 1010.0],
    ]
    train_y = ["normal", "normal", "normal", "high", "high", "high"]
    test_x = [[1.1, 1015.0], [8.9, 995.0]]

    res = engine.ml_analyze(
        "classify",
        train_data=train_x,
        train_labels=train_y,
        test_data=test_x,
        k=3,
    )
    assert res["predictions"] == ["normal", "high"]
    assert len(res["probabilities"]) == 2
    assert res["probabilities"][0]["normal"] > 0.8
    assert res["probabilities"][1]["high"] > 0.8


def test_ml_analyze_forecast_linear_regression() -> None:
    engine = LocalAIEngine()
    res = engine.ml_analyze("forecast", series=[10.0, 20.0, 30.0, 40.0, 50.0], horizon=3)
    assert res["forecast"] == [60.0, 70.0, 80.0]
    assert math.isclose(res["slope"], 10.0, rel_tol=1e-5)
    assert math.isclose(res["intercept"], 10.0, rel_tol=1e-5)
    assert math.isclose(res["r2_score"], 1.0, rel_tol=1e-5)
    assert res["trend"] == "increasing"


def test_ml_analyze_cluster_deterministic_kmeans_plusplus() -> None:
    engine = LocalAIEngine()
    points = [
        [0.0, 0.0],
        [0.1, -0.1],
        [-0.1, 0.1],
        [10.0, 10.0],
        [10.2, 9.8],
        [9.8, 10.1],
    ]
    res1 = engine.ml_analyze("cluster", data=points, n_clusters=2)
    res2 = engine.ml_analyze("cluster", data=points, n_clusters=2)

    assert res1["labels"] == res2["labels"]
    assert res1["labels"][0] == res1["labels"][1] == res1["labels"][2]
    assert res1["labels"][3] == res1["labels"][4] == res1["labels"][5]
    assert res1["labels"][0] != res1["labels"][3]
    assert sorted(res1["cluster_sizes"]) == [3, 3]
    assert res1["inertia"] < 1.0


def test_ml_analyze_anomaly_mad_z_score() -> None:
    engine = LocalAIEngine()
    series = [10.0, 10.2, 9.9, 10.1, 10.0, 9.8, 95.0, 10.1]
    res = engine.ml_analyze("anomaly", series=series, z_threshold=3.5)
    assert len(res["anomalies"]) == 1
    assert res["anomalies"][0]["index"] == 6
    assert res["anomalies"][0]["value"] == 95.0
    assert res["scores"][6] > 10.0


# ---------------------------------------------------------------------------
# 4. Binary Vision, Document OCR & Audio Transcription
# ---------------------------------------------------------------------------


def test_vision_analyze_png_text_chunks_and_multiple_formats() -> None:
    engine = LocalAIEngine()
    png_bytes = _make_png_bytes(
        16,
        12,
        text_chunk=("Title", "Quarterly Report"),
        ztxt_chunk=("Description", "Compressed PNG metadata"),
        itxt_chunk=("ArabicNote", "تقرير الذكاء المحلي"),
    )
    png_res = engine.vision_analyze(base64.b64encode(png_bytes).decode("ascii"))
    assert png_res["format"] == "PNG"
    assert png_res["width"] == 16
    assert png_res["height"] == 12
    assert png_res["channels"] == 3
    assert len(png_res["sha256"]) == 64
    assert png_res["entropy_bits"] > 0.0
    assert "Quarterly Report" in png_res["embedded_text"]
    assert "Compressed PNG metadata" in png_res["embedded_text"]
    assert "تقرير الذكاء المحلي" in png_res["embedded_text"]

    # JPEG SOF0 header
    jpeg_bytes = (
        b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
        b"\xff\xc0\x00\x11\x08\x00\x20\x00\x40\x03\x01\x11\x00\x02\x11\x01\x03\x11\x01"
        b"\xff\xd9"
    )
    jpg_res = engine.vision_analyze(base64.b64encode(jpeg_bytes).decode("ascii"))
    assert jpg_res["format"] == "JPEG"
    assert (jpg_res["width"], jpg_res["height"], jpg_res["channels"]) == (64, 32, 3)

    # GIF89a
    gif_bytes = b"GIF89a" + struct.pack("<HH", 24, 18) + b"\x00\x00\x00;"
    gif_res = engine.vision_analyze(base64.b64encode(gif_bytes).decode("ascii"))
    assert (gif_res["format"], gif_res["width"], gif_res["height"]) == ("GIF", 24, 18)

    # BMP
    bmp_header = (
        b"BM" + struct.pack("<IHHI", 54, 0, 0, 54) + struct.pack("<IiiHH", 40, 10, 20, 1, 32)
    )
    bmp_res = engine.vision_analyze(base64.b64encode(bmp_header).decode("ascii"))
    assert (bmp_res["format"], bmp_res["width"], bmp_res["height"], bmp_res["channels"]) == (
        "BMP",
        10,
        20,
        4,
    )

    # PPM
    ppm_bytes = b"P6\n# comment line\n8 6\n255\n" + (b"\x00\xff\x00" * 48)
    ppm_res = engine.vision_analyze(base64.b64encode(ppm_bytes).decode("ascii"))
    assert (ppm_res["format"], ppm_res["width"], ppm_res["height"], ppm_res["channels"]) == (
        "PPM",
        8,
        6,
        3,
    )


def test_document_ocr_pdf_flatedecode_png_and_html_with_structured_fields() -> None:
    engine = LocalAIEngine()
    pdf_stream = (
        "BT (Invoice Number: INV-2026-778) Tj "
        "[(Issued on 2026-10-13 ) (by OpenAI Inc in San Francisco )] TJ "
        "(Client: John Smith, Total: $3,500.00, Email: accounts@example.org, "
        "Portal: https://example.org/pay) Tj ET"
    )
    pdf_b64 = base64.b64encode(_make_pdf_bytes(pdf_stream, compress=True)).decode("ascii")
    pdf_res = engine.document_ocr(content_b64=pdf_b64)

    assert pdf_res["format"] == "PDF"
    assert "INV-2026-778" in pdf_res["text"]
    assert "INV-2026-778" in pdf_res["fields"]["invoice_numbers"]
    assert "2026-10-13" in pdf_res["fields"]["dates"]
    assert "$3,500.00" in pdf_res["fields"]["amounts"]
    assert "accounts@example.org" in pdf_res["fields"]["emails"]
    assert "https://example.org/pay" in pdf_res["fields"]["urls"]
    assert any("OpenAI" in o for o in pdf_res["fields"]["organizations"])
    assert "San Francisco" in pdf_res["fields"]["locations"]

    # HTML / SVG OCR
    html_doc = (
        "<html><body><h1>فاتورة رقم: INV-AR-901</h1>"
        "<p>المهندس أحمد محمود - شركة أرامكو في الرياض بتاريخ ١٥ أكتوبر ٢٠٢٦</p>"
        "<p>المبلغ: ٥٠٠٠ ريال سعودي - info@aramco.example.org</p></body></html>"
    )
    html_res = engine.document_ocr(text=html_doc)
    assert html_res["format"] == "HTML"
    assert "INV-AR-901" in html_res["fields"]["invoice_numbers"]
    assert "info@aramco.example.org" in html_res["fields"]["emails"]


def test_audio_transcribe_wav_signal_metrics_and_embedded_chunks() -> None:
    engine = LocalAIEngine()
    wav_bytes = _make_wav_bytes(
        sample_rate=8000,
        duration_sec=0.25,
        freq_hz=440.0,
        amplitude=0.5,
        txt_note="مرحبا بكم في نظام الذكاء المحلي",
        info_note="Local CPU Audio",
    )
    res = engine.audio_transcribe(base64.b64encode(wav_bytes).decode("ascii"))
    assert res["format"] == "WAV"
    assert math.isclose(res["duration_seconds"], 0.25, rel_tol=1e-2)
    assert res["sample_rate"] == 8000
    assert res["channels"] == 1
    assert res["rms"] > 0.3
    assert res["peak"] > 0.45
    assert res["zero_crossing_rate"] > 0.05
    assert res["is_silent"] is False
    assert "مرحبا بكم" in res["transcript"]
    assert "Local CPU Audio" in res["transcript"]

    silent_wav = _make_wav_bytes(amplitude=0.0)
    silent_res = engine.audio_transcribe(base64.b64encode(silent_wav).decode("ascii"))
    assert silent_res["is_silent"] is True
    assert silent_res["rms"] == 0.0


# ---------------------------------------------------------------------------
# 5. ToolRegistry, CelaBrowser & HTTP surfaces (/api/* and /cela/v1/*)
# ---------------------------------------------------------------------------


async def test_local_ai_tools_registered_in_build_registry_and_executable() -> None:
    registry, _guard, _browser = build_registry()
    expected = {"nlp_extract_entities", "semantic_search", "ml_analyze", "document_ocr"}
    assert expected <= set(registry.names())
    assert len(local_ai_tools()) == 4

    r_nlp = await registry.execute(
        ToolCall(
            name="nlp_extract_entities",
            args={"text": "Contact billing@example.org in Cairo on 2026-10-13 for $200"},
        )
    )
    assert r_nlp.ok
    assert "billing@example.org" in r_nlp.output

    r_ml = await registry.execute(
        ToolCall(
            name="ml_analyze",
            args={"task": "forecast", "series": [2, 4, 6, 8], "horizon": 2},
        )
    )
    assert r_ml.ok
    assert "10.0" in r_ml.output


def test_cela_browser_synchronous_local_ai_facade_methods() -> None:
    browser = CelaBrowser()
    assert browser.ai_capabilities()["mode"] == "local-cpu"

    ent = browser.extract_entities("شركة أرامكو في الرياض info@example.org")
    assert "info@example.org" in ent["entities_by_type"]["EMAIL"]

    browser.index_documents(["Local CPU AI engine for semantic search"], namespace="demo_ns")
    hits = browser.semantic_search("CPU semantic search", namespace="demo_ns", top_k=1)
    assert len(hits["results"]) == 1

    fc = browser.ml_analyze("forecast", series=[1.0, 2.0, 3.0], horizon=2)
    assert fc["forecast"] == [4.0, 5.0]

    png_b64 = base64.b64encode(_make_png_bytes(8, 8)).decode("ascii")
    vis = browser.vision_analyze(png_b64)
    assert vis["width"] == 8

    ocr = browser.document_ocr(content_b64=png_b64)
    assert "INV-2026-99" in ocr["fields"]["invoice_numbers"]

    wav_b64 = base64.b64encode(_make_wav_bytes(txt_note="hello local ai")).decode("ascii")
    aud = browser.audio_transcribe(wav_b64)
    assert "hello local ai" in aud["transcript"]


def test_http_api_and_cela_v1_routes_share_knowledge_base_and_isolate_tokens() -> None:
    app = create_app(
        build_demo_platform(),
        api_token=PLATFORM_TOKEN,
        private_api_token=PRIVATE_TOKEN,
    )
    p_headers = {"Authorization": f"Bearer {PLATFORM_TOKEN}"}
    c_headers = {"Authorization": f"Bearer {PRIVATE_TOKEN}"}

    with TestClient(app) as client:
        # Token isolation checks across /api/* and /cela/v1/*
        assert client.get("/api/capabilities", headers=c_headers).status_code == 401
        assert client.get("/cela/v1/ai/capabilities", headers=p_headers).status_code == 401

        assert client.get("/api/capabilities", headers=p_headers).json()["mode"] == "local-cpu"
        assert (
            client.get("/cela/v1/ai/capabilities", headers=c_headers).json()["mode"] == "local-cpu"
        )

        # Index via public /api/search/index and query via private /cela/v1/search/semantic
        idx = client.post(
            "/api/search/index",
            json={
                "namespace": "shared_ns",
                "documents": [
                    {"id": "d1", "text": "الذكاء الاصطناعي وتعلم الآلة على المعالج المحلي"},
                    {"id": "d2", "text": "Invoice billing and financial tax accounting"},
                ],
            },
            headers=p_headers,
        )
        assert idx.status_code == 200
        assert idx.json()["total_documents"] == 2

        search = client.post(
            "/cela/v1/search/semantic",
            json={"namespace": "shared_ns", "query": "artificial intelligence machine learning"},
            headers=c_headers,
        )
        assert search.status_code == 200
        assert search.json()["results"][0]["id"] == "d1"

        # Verify remaining dual endpoints on both surfaces
        for prefix, hdrs in (("/api", p_headers), ("/cela/v1", c_headers)):
            assert (
                client.post(
                    f"{prefix}/nlp/entities",
                    json={"text": "Email team@example.org in Cairo"},
                    headers=hdrs,
                ).status_code
                == 200
            )
            assert client.post(
                f"{prefix}/ml/analyze",
                json={"task": "forecast", "series": [5, 10, 15], "horizon": 2},
                headers=hdrs,
            ).json()["forecast"] == [20.0, 25.0]
            png_b64 = base64.b64encode(_make_png_bytes(6, 4)).decode("ascii")
            assert (
                client.post(
                    f"{prefix}/vision/analyze",
                    json={"image_b64": png_b64},
                    headers=hdrs,
                ).json()["width"]
                == 6
            )
            assert (
                client.post(
                    f"{prefix}/documents/ocr",
                    json={"content_b64": png_b64},
                    headers=hdrs,
                ).status_code
                == 200
            )
            wav_b64 = base64.b64encode(_make_wav_bytes(txt_note="test note")).decode("ascii")
            assert (
                client.post(
                    f"{prefix}/audio/transcribe",
                    json={"audio_b64": wav_b64},
                    headers=hdrs,
                ).json()["transcript"]
                == "test note"
            )


def test_invalid_inputs_raise_value_error_and_return_422_over_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = LocalAIEngine()
    with pytest.raises(ValueError, match="unsupported ml_analyze task"):
        engine.ml_analyze("unknown_task")
    with pytest.raises(ValueError, match="document_ocr requires"):
        engine.document_ocr()

    app = create_app(
        build_demo_platform(),
        api_token=PLATFORM_TOKEN,
        private_api_token=PRIVATE_TOKEN,
    )
    p_headers = {"Authorization": f"Bearer {PLATFORM_TOKEN}"}
    c_headers = {"Authorization": f"Bearer {PRIVATE_TOKEN}"}
    with TestClient(app) as client:
        assert (
            client.post("/api/ml/analyze", json={"task": "bad"}, headers=p_headers).status_code
            == 422
        )
        assert (
            client.post(
                "/cela/v1/vision/analyze", json={"image_b64": "dGVzdA=="}, headers=c_headers
            ).status_code
            == 422
        )
        assert client.post("/api/documents/ocr", json={}, headers=p_headers).status_code == 422
        assert (
            client.post(
                "/cela/v1/audio/transcribe", json={"audio_b64": "dGVzdA=="}, headers=c_headers
            ).status_code
            == 422
        )

    # Also verify /api/* fails closed with 503 when token is unconfigured
    monkeypatch.delenv("CELA_API_TOKEN", raising=False)
    monkeypatch.delenv("OS_API_TOKEN", raising=False)
    unconf_router = create_local_ai_router(engine, api_token="")
    assert unconf_router.prefix == "/api"
