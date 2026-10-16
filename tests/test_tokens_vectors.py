"""Tokens, vectors, and the two claims this layer is allowed to make.

Everything here is offline, deterministic, and key-free — the same conditions
``docs/INTEGRATIONS_PLAN.md`` §١٠-أ publishes numbers under. Two assertions are the
reason the module exists at all:

* **stability** — a token id and a packed vector must be reproducible across
  processes, or a chunk-level dedup index is a coin flip;
* **honest units** — every budget number carries the rule that made it, and an
  estimate is only ever compared against the provider's own ``usage.prompt_tokens``.
  There is deliberately **no** test asserting an error bar against a real BPE
  tokenizer: the reference vocabularies are not reachable from this environment, so
  such a test could only assert a number somebody typed in.
"""

from __future__ import annotations

import hashlib
import json

import httpx
import pytest
from os_core.models import OpenAICompatModel, ScriptedModel
from os_core.router import ModelRouter, RouterConfig, TaskProfile
from os_core.tokens import (
    LEGACY_CHARS_PER_TOKEN,
    VOCAB_SIZE,
    TokenCalibration,
    char_divisor,
    count_tokens,
    detect_dominant_script,
    drift,
    estimate,
    estimate_model_tokens,
    normalize,
    split_tokens,
    token_budget_chars,
    token_ids,
    token_spans,
    token_stats,
    truncate_to_token_budget,
    truncate_to_tokens,
)
from os_core.types import Message
from os_core.vectors import (
    HEADER_SIZE,
    RRF_K,
    VectorError,
    cosine,
    embedding_space,
    fuse_rankings,
    l2_normalize,
    pack,
    rank_by_cosine,
    reciprocal_rank_fusion,
    unpack,
)

ARABIC = "إجمالي الفاتورة ١٢٥٠ ريالًا مع ضريبة ١٥٪ من الميزانية السنوية"
ENGLISH = "The total invoice amount is 1250 SAR including a 15% tax on the yearly budget"

# ------------------------------------------------------------------ tokens --


def test_normalize_folds_case_digits_and_marks() -> None:
    assert normalize("RUNBOOK") == "runbook"
    assert normalize("١٢٥٠") == "1250"
    # Diacritics are decoration: two spellings of one word must not split a cluster.
    assert normalize("كِتابُ") == normalize("كتاب")
    # Whitespace is *not* trimmed: folding is per token, and ``split_tokens`` owns
    # boundaries. A normalize() that also stripped would make two functions disagree
    # about who is responsible for the same characters.
    assert normalize("  runbook  ") == "  runbook  "


def test_token_ids_are_stable_and_content_addressed() -> None:
    ids = token_ids(ARABIC)
    assert ids == token_ids(ARABIC), "ids must survive a second call and a second process"
    assert ids == token_ids(ARABIC.replace("  ", " "))
    # The id is a hash of the normalised surface, not a process-local counter.
    assert ids == [
        int.from_bytes(hashlib.sha256(word.encode("utf-8", "surrogatepass")).digest()[:8], "big")
        % VOCAB_SIZE
        for word in split_tokens(ARABIC)
    ]
    assert all(0 <= value < VOCAB_SIZE for value in ids)


def test_case_folding_shares_ids_across_spellings() -> None:
    assert token_ids("Runbook") == token_ids("runbook")
    assert count_tokens("Runbook runbook RUNBOOK") == 3


def test_long_runs_are_split_so_one_line_cannot_blow_up() -> None:
    pieces = split_tokens("أ" * 100)
    assert pieces, "a run of letters is still content"
    assert all(len(piece) <= 24 for piece in pieces)
    assert "".join(pieces) == "أ" * 100, "splitting is lossless on the surface text"


def test_vocab_size_must_be_positive() -> None:
    with pytest.raises(ValueError, match="vocab_size"):
        token_ids("a", vocab_size=0)


def test_script_detection_drives_the_divisor() -> None:
    assert detect_dominant_script(ARABIC) == "ar"
    assert detect_dominant_script(ENGLISH) == "en"
    assert char_divisor("ar") < char_divisor("en"), "Arabic costs more characters per token"
    ar_tokens, ar_source = estimate_model_tokens(ARABIC)
    en_tokens, en_source = estimate_model_tokens(ENGLISH)
    assert ar_source == "chars/2.6[ar]" and en_source == "chars/4[en]"
    assert len(ARABIC) / ar_tokens == pytest.approx(2.6, abs=0.1)
    assert len(ENGLISH) / en_tokens == pytest.approx(4.0, abs=0.1)


def test_estimate_dispatch_names_its_rule_and_refuses_invention() -> None:
    legacy, source = estimate(ENGLISH)
    assert (legacy, source) == (
        len(ENGLISH) // LEGACY_CHARS_PER_TOKEN,
        "chars/4[legacy]",
    )
    assert estimate(ENGLISH, "platform") == estimate_model_tokens(ENGLISH)
    with pytest.raises(ValueError, match="unknown token estimator"):
        estimate(ENGLISH, "bpe")
    assert estimate("")[0] >= 1, "an empty task must not slip past every threshold"


def test_empty_text_counts_zero_where_zero_is_true() -> None:
    assert count_tokens("") == 0
    assert count_tokens("   ...!?   ") == 1, "punctuation-only runs are one token"
    assert estimate_model_tokens("   ") == (0, "empty")


def test_spans_are_ordered_disjoint_slices_of_the_input() -> None:
    text = f"# Title\n\n{ARABIC}\n\n{ENGLISH}\n"
    spans = token_spans(text)
    assert spans, "a mixed document tokenises"
    previous_stop = 0
    for start, stop in spans:
        assert previous_stop <= start < stop <= len(text)
        assert text[start:stop].strip(), "a span never covers only whitespace"
        previous_stop = stop
    # The splitter's losslessness claim: every token slice is verbatim surface text.
    assert all(text in text for text in (text[a:b] for a, b in spans))


def test_truncation_is_verbatim_prefix_in_either_unit() -> None:
    short = truncate_to_tokens(ENGLISH, 6)
    assert short.endswith("…")
    body = short[:-1].rstrip()
    assert ENGLISH.startswith(body), "a truncated snippet is a prefix, never a rebuild"
    assert count_tokens(body) <= 6

    budget = truncate_to_token_budget(ARABIC, 10)
    assert estimate_model_tokens(budget.rstrip("…").rstrip())[0] <= 10
    assert ENGLISH.startswith(truncate_to_token_budget(ENGLISH, 10**6).rstrip("…").rstrip())

    assert truncate_to_token_budget(ENGLISH, 0) == ENGLISH, "0 disables the cap"


def test_token_budget_chars_follows_the_script() -> None:
    assert token_budget_chars(ARABIC, 512) == int(512 * char_divisor("ar") + 0.5)
    assert token_budget_chars(ENGLISH, 512) == int(512 * char_divisor("en") + 0.5)
    assert token_budget_chars(ARABIC, 512) < token_budget_chars(ENGLISH, 512)
    assert token_budget_chars(ENGLISH, 0) == len(ENGLISH), "no budget means no cut"


def test_token_stats_reports_both_units_and_never_conflates_them() -> None:
    stats = token_stats(ARABIC)
    assert stats["chars"] == len(ARABIC)
    assert stats["tokens"] == count_tokens(ARABIC) == stats["words"]
    assert stats["estimated_model_tokens"] == estimate_model_tokens(ARABIC)[0]
    assert stats["tokens"] != stats["estimated_model_tokens"], "two units, two names, one dict"
    assert stats["estimator"] == "chars/2.6[ar]"
    assert stats["unique_tokens"] <= stats["tokens"]


def test_drift_is_directional_and_refuses_to_invent() -> None:
    assert drift(100, 130)["ratio"] == 1.3
    assert drift(100, 130)["available"] is True
    assert drift(100, 0)["available"] is False
    assert drift(0, 50)["available"] is False


# ----------------------------------------------------------------- vectors --


def test_packed_header_carries_the_dimension() -> None:
    blob = pack([0.5, -0.25, 0.0, 1.0])
    assert blob[:4] == b"CEV1" and len(blob) == HEADER_SIZE + 4 * 4
    assert unpack(blob) == [0.5, -0.25, 0.0, 1.0]
    with pytest.raises(VectorError, match="dimension"):
        unpack(blob, expect_dim=3)
    with pytest.raises(VectorError, match="CEV1"):
        unpack(b"JUNK" + b"\x00" * 8)
    assert unpack(None) == [] and unpack(b"") == []


def test_cosine_refuses_mismatched_lengths_instead_of_guessing() -> None:
    a = [1.0, 0.0, 0.0]
    assert cosine(a, a) == pytest.approx(1.0)
    assert cosine(a, [0.0, 1.0, 0.0]) == pytest.approx(0.0)
    assert cosine(a, [1.0, 0.0]) == 0.0, "different space, zero similarity — no partial dot product"
    assert cosine([], []) == 0.0


def test_l2_normalize_roundtrip() -> None:
    values = l2_normalize([3.0, 4.0])
    assert values[0] ** 2 + values[1] ** 2 == pytest.approx(1.0)
    assert l2_normalize([0.0, 0.0]) == [0.0, 0.0]


def test_rank_by_cosine_breaks_ties_by_id() -> None:
    query = [1.0, 0.0]
    ranked = rank_by_cosine(
        query,
        [
            ("b", [0.0, 1.0]),
            ("a", [1.0, 0.0]),
            ("c", [1.0, 0.0]),
            ("d", [1.0, 0.0]),
        ],
    )
    assert [key for key, _score in ranked] == ["a", "c", "d", "b"], "ties fall to the id"
    assert ranked[0][1] == pytest.approx(1.0) and ranked[-1][1] == 0.0
    # ``min_score`` is the dense-threshold knob every layer reuses.
    assert [
        key
        for key, _ in rank_by_cosine(query, [("a", [1.0, 0.0]), ("b", [0.0, 1.0])], min_score=0.5)
    ] == ["a"]


def test_reciprocal_rank_fusion_rewards_agreement() -> None:
    fused = reciprocal_rank_fusion({"sparse": ["x", "y"], "dense": ["y", "z"]})
    scores = dict(fused)
    assert scores["y"] > scores["x"] and scores["y"] > scores["z"]
    # Scores are rounded on purpose (a report must diff cleanly), so the assertion
    # compares the same rounded quantity rather than an exact float.
    assert scores["x"] == round(1.0 / (RRF_K + 1), 6)
    assert scores["y"] == round(1.0 / (RRF_K + 2) + 1.0 / (RRF_K + 1), 6)


def test_fuse_rankings_returns_positions_for_the_arm_report() -> None:
    fused, positions = fuse_rankings({"sparse": ["a", "b", "c"], "dense": ["b", "c", "d"]})
    assert next(key for key, _ in fused) == "b"
    assert positions["sparse"]["a"] == 1 and positions["dense"]["d"] == 3, "ranks are 1-based"
    assert "a" not in positions["dense"] and "d" not in positions["sparse"]
    assert set(positions) == {"sparse", "dense"}


def test_embedding_space_tag_binds_model_and_dimension() -> None:
    class Backend:
        def __init__(self, model: str, dim: int) -> None:
            self.info = type("Info", (), {"model": model, "dim": dim})()

    assert embedding_space(Backend("hash-v1", 512)) == "hash-v1@512"
    assert embedding_space(Backend("hash-v1", 64)) != embedding_space(Backend("hash-v1", 512))
    assert embedding_space(None) is None, "None means do not filter, never match-all"
    assert embedding_space(object()) is None, "an untagged backend is no space"

    class NoDim:
        name = "legacy"

    assert embedding_space(NoDim()) == "legacy"


# ------------------------------------------------------------------ router --


def test_router_default_estimator_is_the_one_numbers_were_published_with() -> None:
    router = ModelRouter()
    profile = router.profile(ENGLISH)
    assert profile.prompt_tokens == len(ENGLISH) // 4
    assert profile.token_source == "chars/4[legacy]"
    assert isinstance(profile, TaskProfile)


def test_router_platform_estimator_changes_the_count_but_not_by_accident() -> None:
    router = ModelRouter(config=RouterConfig(token_estimator="platform"))
    counted = router.profile(ARABIC)
    legacy = ModelRouter().profile(ARABIC)
    assert counted.prompt_tokens > legacy.prompt_tokens, "Arabic costs more than chars/4 admits"
    assert counted.token_source == "chars/2.6[ar]"
    # One text, two units, two tiers: 2 440 Arabic characters are 610 tokens at
    # chars/4 and 938 at chars/2.6, so a threshold inside that gap is a routing
    # decision the *estimator* makes, not the model. Which is why the unit is named on
    # the profile instead of being assumed by whoever reads the tier later.
    text = ARABIC * 40
    tight = RouterConfig(token_estimator="platform", cheap_planner_max_tokens=700)
    roomy = RouterConfig(token_estimator="chars4", cheap_planner_max_tokens=700)
    assert ModelRouter(config=tight).profile(text).is_planning is True
    assert ModelRouter(config=roomy).profile(text).is_planning is False


def test_router_rejects_an_unknown_estimator_loudly(monkeypatch) -> None:
    for name, value in (
        ("CELA_TOKEN_ESTIMATOR", "gpt2-bpe"),
        ("CELA_TOKEN_BUDGET", "lots"),
    ):
        monkeypatch.setenv(name, value)
        with pytest.raises(ValueError, match=name):
            RouterConfig.from_env()


def test_router_from_env_honours_both_knobs(monkeypatch) -> None:
    monkeypatch.setenv("CELA_TOKEN_ESTIMATOR", "platform")
    monkeypatch.setenv("CELA_TOKEN_BUDGET", "12345")
    config = RouterConfig.from_env()
    assert config.token_estimator == "platform" and config.token_budget == 12345
    info = config.estimator_info()
    assert info["estimator"] == "platform"
    assert info["counts_billed_tokens"] is False, "a routing estimate is not a bill"


def test_empty_environment_keeps_the_published_defaults(monkeypatch) -> None:
    monkeypatch.delenv("CELA_TOKEN_ESTIMATOR", raising=False)
    monkeypatch.delenv("CELA_TOKEN_BUDGET", raising=False)
    config = RouterConfig.from_env()
    assert config.token_estimator == "chars4" and config.token_budget == 200_000


# -------------------------------------------------------------- calibration --


def _completion(prompt_tokens: int) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": "noted"}, "finish_reason": "stop"}],
            "model": "test-model",
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": 7},
        },
    )


async def test_calibration_measures_drift_against_reported_usage() -> None:
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return _completion(prompt_tokens=900)

    model = OpenAICompatModel(
        base_url="http://model.invalid/v1", transport=httpx.MockTransport(handler)
    )
    report = model.calibration_report()
    assert report["samples"] == 0 and report["drift_ratio"] is None
    assert report["direction"] == "unknown", "no samples must not read as perfect calibration"

    await model.complete([Message(role="user", content=ENGLISH * 12)])
    report = model.calibration_report()
    assert report["samples"] == 1 and report["missing_usage"] == 0
    assert report["estimators"] == ["chars/4[legacy]"]
    estimated = report["estimated_total"]
    assert report["observed_total"] == 900
    assert report["drift_ratio"] == round(900 / estimated, 4)
    expected = "under-counted" if estimated < 900 else "over-counted"
    assert report["direction"] == expected
    # The estimate is taken over the serialized body — the thing that was billed — so
    # it must exceed the bare message text and stay in the same order of magnitude.
    # Asserted as a range on purpose: exact JSON spacing is the transport's business,
    # and pinning it here would make this test fail for a formatting change.
    raw = len(json.dumps(seen[0], ensure_ascii=False))
    assert len(ENGLISH) * 12 // 4 < estimated <= raw // 4 + 4


async def test_calibration_survives_a_provider_that_reports_nothing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "model": "test-model",
            },
        )

    model = OpenAICompatModel(
        base_url="http://model.invalid/v1", transport=httpx.MockTransport(handler)
    )
    response = await model.complete([Message(role="user", content="hi")])
    assert response.usage.prompt_tokens == 0
    report = model.calibration_report()
    assert report["requests"] == 1 and report["samples"] == 0
    assert report["missing_usage"] == 1
    assert "unmeasured" in report["note"]


async def test_calibration_window_bounds_memory_under_traffic() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return _completion(prompt_tokens=100 + calls["n"])

    model = OpenAICompatModel(
        base_url="http://model.invalid/v1",
        transport=httpx.MockTransport(handler),
        calibration=TokenCalibration(keep=3),
    )
    for _ in range(7):
        await model.complete([Message(role="user", content="ping")])
    report = model.calibration_report()
    assert report["requests"] == 7, "counters see every request…"
    assert report["samples"] == 3, "…the window keeps only the most recent"


async def test_scripted_usage_is_never_recorded_as_a_measurement() -> None:
    """The offline model invents its own token counts, so it must not "calibrate".

    A zero-drift row from a fabricated usage block would be the most convincing
    possible way to publish nothing.
    """
    model = ScriptedModel()
    await model.complete([Message(role="user", content=ENGLISH)])
    assert model.call_count == 1
    assert not hasattr(model, "calibration"), "no recorder, no temptation"


def test_calibration_report_is_json_safe() -> None:
    calibration = TokenCalibration()
    calibration.record(estimated=100, observed=100, estimator="chars/4[legacy]")
    payload = json.dumps(calibration.report())
    assert json.loads(payload)["direction"] == "matched"
    assert calibration.record(estimated=5, observed=0)["available"] is False
