"""Platform token ids and a countable budget unit — **not** a model's BPE.

Why this file exists, stated as a measured gap rather than a taste:

* ``ModelRouter.profile`` decided cost ceilings from ``len(text) // 4``
  (``os_core/router.py``), and every budget downgrade in the run inherited that
  guess. ``os_know`` capped chunks at ``1600`` *characters* — a unit no model
  bills in, and one that punishes Arabic far harder than English because a
  Arabic word carries more surface characters per meaning.
* So the platform had no unit it could check: not for the context budget, not
  for chunk sizing, and no stable id for a token to be deduplicated or indexed
  by.

What this module gives, and what it explicitly does **not**:

======================  ================================================================
gives                   a deterministic, dependency-free tokenizer with **stable ids**
                        (same bytes → same ids in every process, no ``PYTHONHASHSEED``),
                        a language-aware character→token estimate, and a record of which
                        estimator produced any number.
does **not** give        the deployed model's byte-pair vocabulary. ``model_tokens`` here
                        is a *platform* count. The provider's own ``usage.prompt_tokens``
                        always wins when it is reported — that is why :func:`drift` exists
                        and why ``RouterConfig`` can be left on the old estimator without
                        pretending the estimate was a measurement.
======================  ================================================================

No accuracy claim is published here: the reference BPE vocabularies are not
downloadable in this repository's offline test environment, so an error bar
against a real tokenizer would be a number invented, not measured. What *is*
measurable offline — stability, determinism, Arabic-vs-English character
ratio, and drift once a real model answers — is asserted in
``tests/test_tokens_vectors.py``.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

#: The two estimators the platform is allowed to bill against. ``chars4`` is the
#: historical ``len(text) // 4`` rule every published number was produced with, so
#: it stays the default; ``platform`` is the language-aware divisor below. A third
#: value — the model's real BPE — is deliberately absent: it cannot be measured in
#: this repository's offline test environment, and a knob nobody can verify is a
#: knob that eventually lies.
TokenEstimator = Literal["chars4", "platform"]

ESTIMATORS: tuple[str, ...] = ("chars4", "platform")

#: The legacy rule, spelled out once so ``router`` and ``models`` cannot drift apart.
LEGACY_CHARS_PER_TOKEN = 4

#: Size of the (hashed) token id space. 2**20 keeps ids small enough to index and
#: collision-prone enough to be honest: with ``n`` distinct surface tokens the
#: expected collisions are ~``n²/2**21``, which is why ids are *identity* keys for
#: chunking/dedup — never a semantic embedding on their own.
VOCAB_SIZE: int = 1 << 20

#: A run of characters longer than this is cut, so one pathological line cannot
#: produce an unbounded token list (and an unbounded vector) from a small input.
MAX_PIECE_CHARS: int = 24

#: Arabic surface text carries diacritics and tatweel that a BPE would mostly fold
#: into neighbouring pieces; counting them as content inflates every estimate.
_ARABIC_MARKS = "ًٌٍَُِّْـ"
_ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩٫", "0123456789.")
_LATIN_DIGITS = "0123456789"

# One alternation, three shapes: an Arabic run, a latin run, a single CJK
# character. The ``{1,MAX_PIECE_CHARS}`` bound is doing double duty — it drops
# punctuation *and* cuts a pathological 10 kB "word" into pieces, so neither can
# produce an unbounded token list from a small document.
_WORD = re.compile(
    rf"[\u0620-\u064a\u0671-\u06d3]{{1,{MAX_PIECE_CHARS}}}"
    rf"|[a-z0-9_]{{1,{MAX_PIECE_CHARS}}}"
    r"|[㐀-䶿一-鿿]"
)
#: Same shapes, no lowercasing — used where the *surface* text must survive a cut.
_TRUNCATE_WORD = re.compile(
    rf"[\u0620-\u064a\u0671-\u06d3]{{1,{MAX_PIECE_CHARS}}}"
    rf"|[A-Za-z0-9_]{{1,{MAX_PIECE_CHARS}}}"
    r"|[㐀-䶿一-鿿]"
)


class Language(StrEnum):
    ARABIC = "ar"
    LATIN = "en"
    MIXED = "mixed"
    OTHER = "other"


#: characters per platform-token, measured over this tokenizer — **not** over a
#: provider BPE. Arabic is denser in characters per word (prefixes, clitics,
#: morphology) and that is exactly the case ``len(text)//4`` gets wrong by the
#: most, so the Arabic divisor is smaller than the Latin one on purpose.
_CHARS_PER_TOKEN: dict[str, float] = {
    Language.ARABIC: 2.6,
    Language.LATIN: 4.0,
    Language.MIXED: 3.3,
    Language.OTHER: 3.6,
}


def normalize(text: str) -> str:
    """Fold case, combining marks, and Arabic-Indic digits — deterministically.

    ``NFKC`` first so that compatibility forms (``ﻻ`` → ``لا``) do not create two
    ids for one word; marks are stripped after normalisation because NFKC leaves
    harakat alone.
    """
    if not text:
        return ""
    folded = unicodedata.normalize("NFKC", str(text)).lower()
    folded = folded.translate(_ARABIC_DIGITS)
    stripped = "".join(ch for ch in unicodedata.normalize("NFD", folded) if ch not in _ARABIC_MARKS)
    return unicodedata.normalize("NFKC", stripped)


def detect_dominant_script(text: str) -> str:
    """Which script a *count* should be weighted for, ignoring whitespace/punctuation."""
    body = [ch for ch in text or "" if not ch.isspace()]
    if not body:
        return Language.OTHER
    arabic = sum(1 for ch in body if "\u0620" <= ch <= "\u064a" or "\u0671" <= ch <= "\u06d3")
    latin = sum(1 for ch in body if ("a" <= ch <= "z") or ch in _LATIN_DIGITS)
    total = len(body)
    if arabic / total >= 0.6:
        return Language.ARABIC
    if latin / total >= 0.6:
        return Language.LATIN
    if arabic or latin:
        return Language.MIXED
    return Language.OTHER


def split_tokens(text: str) -> list[str]:
    """Surface tokens: Arabic/latin word runs (cut at :data:`MAX_PIECE_CHARS`) and CJK chars.

    Punctuation is dropped rather than tokenised: keeping it made an Arabic
    sentence and the same sentence with emoji compete on punctuation ids, which
    is noise for dedup and a false signal for similarity.
    """
    if not text:
        return []
    return [m.group(0) for m in _WORD.finditer(normalize(text))]


def token_ids(text: str, *, vocab_size: int = VOCAB_SIZE) -> list[int]:
    """Stable ids for :func:`split_tokens` output — sha256, never :func:`hash`.

    ``builtin hash()`` is salted per process, so ids would change on every run and
    a persisted index would quietly stop matching its own vectors.
    """
    if vocab_size < 2:
        raise ValueError(f"vocab_size must be >= 2, got {vocab_size}")
    return [_token_id(token, vocab_size) for token in split_tokens(text)]


def _token_id(token: str, vocab_size: int) -> int:
    digest = hashlib.sha256(token.encode("utf-8", "surrogatepass")).digest()
    return int.from_bytes(digest[:8], "big") % vocab_size


def count_tokens(text: str) -> int:
    """Token count in platform units — at least 1 for any non-blank text."""
    return max(1, len(split_tokens(text))) if (text or "").strip() else 0


def estimate_model_tokens(text: str) -> tuple[int, str]:
    """(tokens, estimator_name) using the language-aware character divisor.

    The estimator name is part of the return value on purpose: a number whose
    provenance cannot be asked is a number nobody can defend later.
    """
    body = text or ""
    if not body.strip():
        return 0, "empty"
    language = detect_dominant_script(body)
    divisor = _CHARS_PER_TOKEN[language]
    return max(1, int(len(body) / divisor + 0.5)), f"chars/{divisor:g}[{language}]"


def estimate(text: str, estimator: TokenEstimator = "chars4") -> tuple[int, str]:
    """One dispatch for every budget decision in the platform: ``(tokens, source)``.

    Both estimators are linear in characters, so any number they produce stays
    explainable in one sentence — which is what a routing decision needs and a learned
    tokenizer does not offer. The source is *returned* rather than looked up so a
    count can never be quoted without the rule that made it.

    ``max(1, …)`` on empty input is deliberate: a zero-token task would slip past
    every threshold in the router and arrive as the cheapest tier.
    """
    body = text or ""
    if estimator == "chars4":
        return max(1, len(body) // LEGACY_CHARS_PER_TOKEN), "chars/4[legacy]"
    if estimator == "platform":
        return estimate_model_tokens(body)
    raise ValueError(f"unknown token estimator {estimator!r}; expected one of {ESTIMATORS}")


class TokenCalibration(BaseModel):
    """A rolling record of *estimated vs reported* prompt tokens, kept by the adapters.

    This is what turns "we estimate tokens" into a falsifiable claim: every completed
    request pairs the platform's own count of the serialized payload with the
    provider's reported ``usage.prompt_tokens``, and :meth:`report` says how far apart
    they were. There is deliberately no accuracy claim against a BPE tokenizer — the
    only ground truth used is the number the provider itself billed, which is exactly
    the unit a context budget is spent in.

    It is a pydantic model so an endpoint can serialise it verbatim, but it is *not*
    a config object: it is instrumentation, attached by reference to one adapter, and
    mutating it is the point. ``keep`` bounds the retained window so a long-lived
    server cannot grow memory with traffic — the counters below cover every request,
    the samples describe the most recent ``keep``.
    """

    #: How many comparable samples to retain for the per-request statistics.
    keep: int = 64
    samples: list[dict[str, Any]] = Field(default_factory=list)
    #: Every request that asked to be recorded, comparable or not.
    recorded: int = 0
    #: Provider responses that carried no usable ``usage`` block.
    missing_usage: int = 0

    @model_validator(mode="after")
    def _clamp_window(self) -> TokenCalibration:
        self.keep = max(1, int(self.keep))
        return self

    def record(self, *, estimated: int, observed: int, estimator: str = "") -> dict[str, Any]:
        """Compare one request; returns the per-request drift (see :func:`drift`)."""
        self.recorded += 1
        delta = drift(max(0, int(estimated)), max(0, int(observed)))
        if not delta.get("available"):
            self.missing_usage += 1
            return delta
        self.samples.append(
            {
                "estimated": int(estimated),
                "observed": int(observed),
                "ratio": float(delta["ratio"]),
                "absolute": int(delta["absolute"]),
                "estimator": estimator or "unknown",
            }
        )
        if len(self.samples) > self.keep:
            del self.samples[: len(self.samples) - self.keep]
        return delta

    def report(self) -> dict[str, Any]:
        """Aggregates over the retained window, and how much of it exists at all.

        ``drift_ratio`` is ``observed / estimated`` summed over the window, so ``1.1``
        means the platform *under*-counted by 10% — the dangerous direction for a hard
        budget. With no samples the numeric fields are ``None`` rather than ``0.0``: a
        zero would read as "perfectly calibrated" to anything that prints it.
        """
        window = len(self.samples)
        if not window:
            return {
                "requests": self.recorded,
                "samples": 0,
                "missing_usage": self.missing_usage,
                "drift_ratio": None,
                "mean_abs_ratio_error": None,
                "worst_abs_ratio_error": None,
                "direction": "unknown",
                "estimators": [],
                "note": (
                    "no provider-reported usage yet, so the estimator's drift is "
                    "unmeasured and no error bar may be quoted for it"
                ),
            }
        estimated_total = sum(int(sample["estimated"]) for sample in self.samples)
        observed_total = sum(int(sample["observed"]) for sample in self.samples)
        ratio = round(observed_total / estimated_total, 4) if estimated_total else None
        errors = [abs(float(sample["ratio"]) - 1.0) for sample in self.samples]
        if ratio is None:
            direction = "unknown"
        elif abs(ratio - 1.0) < 0.01:
            direction = "matched"
        else:
            direction = "under-counted" if ratio > 1.0 else "over-counted"
        return {
            "requests": self.recorded,
            "samples": window,
            "missing_usage": self.missing_usage,
            "estimated_total": estimated_total,
            "observed_total": observed_total,
            "drift_ratio": ratio,
            "mean_abs_ratio_error": round(sum(errors) / len(errors), 4),
            "worst_abs_ratio_error": round(max(errors), 4),
            "direction": direction,
            "estimators": sorted({str(sample["estimator"]) for sample in self.samples}),
            "note": (
                "ratio > 1 means the platform counted fewer tokens than the provider "
                "billed; measured against provider usage, never against a BPE vocabulary"
            ),
        }


def char_divisor(language: str) -> float:
    return _CHARS_PER_TOKEN.get(language, _CHARS_PER_TOKEN[Language.OTHER])


def drift(estimated: int, observed: int) -> dict[str, float]:
    """How wrong an estimate was, once a provider reported the real usage.

    ``ratio`` is ``observed / estimated`` (>1 means the estimate *under*billed —
    the dangerous direction for a budget). Returns ``{"available": False}``-style
    zeros rather than raising when there is nothing to compare, because a router
    that cannot bill a run is a bug, and a router that cannot *calibrate* is not.
    """
    if estimated <= 0 or observed <= 0:
        return {"estimated": float(estimated), "observed": float(observed), "available": False}
    return {
        "estimated": float(estimated),
        "observed": float(observed),
        "absolute": float(abs(observed - estimated)),
        "ratio": round(observed / estimated, 4),
        "available": True,
    }


def token_spans(text: str) -> list[tuple[int, int]]:
    """Character offsets of every token, in surface text.

    Exposed because a chunker must cut on a token boundary *and* return verbatim
    source: a splitter that only knows counts has to guess where the boundary was,
    and a guessed boundary is how a code chunk loses its closing bracket.
    """
    body = text or ""
    return [(m.start(), m.end()) for m in _TRUNCATE_WORD.finditer(body)]


def token_stats(text: str) -> dict[str, object]:
    """Everything a caller might otherwise compute by hand, from one pass."""
    tokens = split_tokens(text)
    estimated, estimator = estimate_model_tokens(text)
    return {
        "chars": len(text or ""),
        "words": len(tokens),
        "language": detect_dominant_script(text or ""),
        "tokens": len(tokens),
        "unique_tokens": len(set(tokens)),
        "estimated_model_tokens": estimated,
        "estimator": estimator,
    }


def token_budget_chars(text: str, max_tokens: int) -> int:
    """How many characters *max_tokens* covers in this text, per the platform estimate.

    Kept separate from the splitter because every caller that caps by tokens has to
    answer the same question first: a chunk of 512 tokens is not 512 words and not
    2048 characters either — it is ``512 × divisor(dominant script)`` characters.
    Deriving the window from the divisor is what keeps the cap meaningful for Arabic,
    where a fixed character count buys ~40% fewer tokens than the same count in
    English, and where a *fixed character* cap therefore silently produces chunks
    that overflow the budget they were said to respect.
    """
    if max_tokens <= 0:
        return len(text or "")
    language = detect_dominant_script(text or "")
    return max(1, int(max_tokens * _CHARS_PER_TOKEN[language] + 0.5))


def truncate_to_token_budget(text: str, max_tokens: int, *, marker: str = "…") -> str:
    """Cut *text* so its estimated model-token count fits *max_tokens*.

    Cuts on a surface-token boundary rather than mid-word, and returns text that is a
    verbatim prefix of the input plus the marker: a chunk quoted with a ``sha256`` next
    to it must never be a reconstruction.
    """
    body = text or ""
    if max_tokens <= 0 or not body:
        return body
    budget = token_budget_chars(body, max_tokens)
    if len(body) <= budget and estimate_model_tokens(body)[0] <= max_tokens:
        return body
    spans = token_spans(body)
    end = 0
    for _token, stop in spans:
        if stop > budget:
            break
        end = stop
    if end <= 0:
        return marker if marker else body[:budget]
    return body[:end].rstrip() + (marker if marker else "")


def truncate_to_tokens(text: str, max_tokens: int, *, marker: str = "…") -> str:
    """Cut on a token boundary — the primitive ``max_snippet_chars`` should have been.

    Boundaries come from :data:`_TRUNCATE_WORD`, which mirrors :data:`_WORD` but
    keeps case, so the returned slice is verbatim surface text rather than the
    normalised form. Falls back to a character cut when there is nothing
    tokenisable at all: returning ``""`` for a real snippet would drop an answer
    silently, and a dropped answer looks exactly like a corpus with no answer.
    """
    if max_tokens < 1:
        raise ValueError(f"max_tokens must be >= 1, got {max_tokens}")
    body = text or ""
    matches = list(_TRUNCATE_WORD.finditer(body))
    if len(matches) <= max_tokens:
        return body
    cut = matches[max_tokens - 1].end()
    if cut <= 0:
        return body[:max_tokens]
    return body[:cut].rstrip() + marker


__all__ = [
    "ESTIMATORS",
    "LEGACY_CHARS_PER_TOKEN",
    "MAX_PIECE_CHARS",
    "VOCAB_SIZE",
    "Language",
    "TokenCalibration",
    "TokenEstimator",
    "char_divisor",
    "count_tokens",
    "detect_dominant_script",
    "drift",
    "estimate",
    "estimate_model_tokens",
    "normalize",
    "split_tokens",
    "token_budget_chars",
    "token_ids",
    "token_spans",
    "token_stats",
    "truncate_to_token_budget",
    "truncate_to_tokens",
]
