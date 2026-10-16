"""LocalAIEngine — zero-cost, offline-first CPU intelligence engine.

Provides deterministic, pure-Python implementations of:

* Arabic + English NLP (`normalise_arabic`, `detect_language`, `extract_entities`)
* Hybrid cross-lingual semantic search (light Arabic/English stemming + TF-IDF +
  character 3/4-grams + bilingual semantic concept clusters)
* Classical ML & time-series analysis (`classify` k-NN, `forecast` OLS linear
  regression, `cluster` deterministic k-means++, `anomaly` MAD Z-score)
* Binary computer vision inspection (`PNG` with `tEXt`/`iTXt`/`zTXt`, `JPEG`,
  `GIF`, `BMP`, `PPM`)
* Document OCR & structured field extraction (`PDF` with `Tj`/`TJ` and
  `FlateDecode`, `PNG` text chunks, `HTML`/`SVG`, and plain text)
* `RIFF/WAVE` audio signal analysis & embedded `TXT `/`LIST` transcript extraction

Runs immediately on CPU with zero external network calls and zero per-request cost,
while probing optional heavy accelerators (`scikit-learn`, `spacy`,
`sentence-transformers`, `faiss`, `opencv`, `paddleocr`, `faster-whisper`).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import html
import importlib.util
import math
import re
import statistics
import struct
import zlib
from collections import Counter
from collections.abc import Sequence
from typing import Any

from os_core.types import RiskLevel

from os_tools.registry import Tool

# ---------------------------------------------------------------------------
# 1. Arabic & multilingual text normalisation and language detection
# ---------------------------------------------------------------------------

_INDIC_DIGITS_MAP = str.maketrans(
    "٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹٪",
    "01234567890123456789%",
)
_ARABIC_DIACRITICS_RE = re.compile(r"[\u064B-\u065F\u0670\u06D6-\u06ED\u0640]")
_ALEF_VARIANTS_RE = re.compile(r"[أإآٱ]")
_MULTI_SPACE_RE = re.compile(r"[ \t]+")

_URL_RE = re.compile(r"https?://[^\s<>\"'،؛)]+", re.IGNORECASE)
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_PHONE_RE = re.compile(
    r"(?:(?:\+|00)\d{1,3}[\s.-]?\(?\d{1,4}\)?[\s.-]?\d{3,4}[\s.-]?\d{3,4}"
    r"|\b01[0125]\d{8}\b"
    r"|\b05\d{8}\b"
    r"|\b\d{3}-\d{3}-\d{4}\b)"
)

_EN_STOPWORDS: frozenset[str] = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "has",
        "have",
        "he",
        "in",
        "is",
        "it",
        "its",
        "of",
        "on",
        "or",
        "that",
        "the",
        "their",
        "this",
        "to",
        "was",
        "were",
        "will",
        "with",
        "we",
        "you",
        "they",
        "our",
        "your",
        "into",
        "about",
        "over",
        "after",
        "before",
        "between",
        "under",
        "during",
    }
)

_AR_STOPWORDS: frozenset[str] = frozenset(
    {
        "في",
        "من",
        "إلى",
        "الى",
        "على",
        "عن",
        "مع",
        "هذا",
        "هذه",
        "ذلك",
        "تلك",
        "الذي",
        "التي",
        "الذين",
        "كان",
        "كانت",
        "يكون",
        "تكون",
        "تم",
        "قد",
        "كل",
        "بعض",
        "أو",
        "او",
        "ثم",
        "بل",
        "لكن",
        "حتى",
        "حتي",
        "منذ",
        "خلال",
        "بين",
        "فوق",
        "تحت",
        "بعد",
        "قبل",
        "عند",
        "نحو",
        "حول",
        "هو",
        "هي",
        "هم",
        "نحن",
        "أنا",
        "انا",
        "أن",
        "ان",
        "إن",
        "لا",
        "لم",
        "لن",
        "ما",
        "هل",
        "كيف",
        "متى",
        "متي",
        "أين",
        "اين",
        "كما",
        "أيضا",
        "ايضا",
    }
)


def normalise_arabic(text: str) -> str:
    """Normalise Arabic orthography and convert Indic digits (٠-٩ / ۰-۹) to ASCII."""
    if not text:
        return ""
    out = text.translate(_INDIC_DIGITS_MAP)
    out = _ARABIC_DIACRITICS_RE.sub("", out)
    out = _ALEF_VARIANTS_RE.sub("ا", out)
    out = out.replace("ى", "ي")
    out = _MULTI_SPACE_RE.sub(" ", out)
    return out.strip()


def _is_arabic_char(ch: str) -> bool:
    code = ord(ch)
    return (
        0x0600 <= code <= 0x06FF or 0x0750 <= code <= 0x077F or 0x08A0 <= code <= 0x08FF
    ) and ch.isalpha()


def _is_latin_char(ch: str) -> bool:
    return ("a" <= ch <= "z") or ("A" <= ch <= "Z")


def _is_han_char(ch: str) -> bool:
    """CJK ideographs — the script that decides whether a page is Chinese.

    Han characters are neither Arabic nor Latin, so before this existed a
    Chinese sentence counted zero in both buckets and fell through the
    "total == 0" branch as ``'en'``. The reach layer reads Chinese-language
    platforms (ADR-0013), which is what made that misclassification matter.
    """
    code = ord(ch)
    return (
        0x4E00 <= code <= 0x9FFF  # CJK Unified Ideographs
        or 0x3400 <= code <= 0x4DBF  # Extension A
        or 0xF900 <= code <= 0xFAFF  # Compatibility Ideographs
    ) and ch.isalpha()


def detect_language(text: str) -> str:
    """Detect whether *text* is ``'ar'``, ``'en'``, ``'zh'``, or ``'mixed'``.

    URLs and email addresses are stripped before counting script characters so
    an Arabic sentence containing a URL or email is not misclassified. Han
    characters make a page Chinese-calibrated rather than English: the reach
    layer reads CJK platforms, and a Chinese page labelled ``'en'`` would route
    to the English analysers with no warning (ADR-0013).
    """
    cleaned = _URL_RE.sub(" ", text or "")
    cleaned = _EMAIL_RE.sub(" ", cleaned)
    ar_count = sum(1 for ch in cleaned if _is_arabic_char(ch))
    en_count = sum(1 for ch in cleaned if _is_latin_char(ch))
    zh_count = sum(1 for ch in cleaned if _is_han_char(ch))
    total = ar_count + en_count + zh_count
    if total == 0:
        return "en"
    zh_ratio = zh_count / total
    ar_ratio = ar_count / total
    if zh_ratio >= 0.5:
        return "zh"
    if ar_ratio >= 0.8:
        return "ar"
    if ar_ratio <= 0.2 and zh_ratio < 0.5:
        return "en"
    return "mixed"


# ---------------------------------------------------------------------------
# 2. Light morphological stemming & cross-lingual semantic concept clusters
# ---------------------------------------------------------------------------

_AR_BROKEN_PLURALS: dict[str, str] = {
    "فواتير": "فاتور",
    "فاتوره": "فاتور",
    "فاتورة": "فاتور",
    "مدفوعات": "دفع",
    "دفعات": "دفع",
    "عملاء": "عميل",
    "زبائن": "عميل",
    "ارباح": "ربح",
    "مبيعات": "بيع",
    "ايرادات": "ايراد",
    "عوائد": "ايراد",
    "ضرائب": "ضريب",
    "ضريبه": "ضريب",
    "بيانات": "بيان",
    "معلومات": "معلوم",
    "نماذج": "نموذج",
    "خوادم": "خادم",
    "سيرفرات": "خادم",
    "حواسيب": "حاسوب",
    "شبكات": "شبك",
    "قواعد": "قاعد",
    "تحليلات": "تحليل",
    "توقعات": "توقع",
    "تنبؤات": "توقع",
    "شذوذ": "شاذ",
    "مخاطر": "خطر",
    "هجمات": "هجوم",
    "ثغرات": "ثغر",
    "مستندات": "مستند",
    "وثائق": "وثيق",
    "ملفات": "ملف",
    "صور": "صور",
    "اصوات": "صوت",
    "تسجيلات": "تسجيل",
    "خوارزميات": "خوارزم",
}

_AR_PREFIXES: tuple[str, ...] = (
    "وال",
    "بال",
    "كال",
    "فال",
    "لل",
    "ال",
)
_AR_SUFFIXES: tuple[str, ...] = (
    "ات",
    "ون",
    "ين",
    "ان",
    "ها",
    "هم",
    "هن",
    "كم",
    "نا",
    "يه",
    "ية",
    "ه",
    "ة",
    "ي",
)

_EN_SUFFIXES: tuple[str, ...] = (
    "ingly",
    "edly",
    "ation",
    "ition",
    "tion",
    "sion",
    "ment",
    "ness",
    "able",
    "ible",
    "ical",
    "ity",
    "ful",
    "ous",
    "ive",
    "ing",
    "ies",
    "es",
    "ed",
    "er",
    "ly",
    "s",
)


def _stem_arabic(word: str) -> str:
    w = normalise_arabic(word).replace("ة", "ه")
    if w in _AR_BROKEN_PLURALS:
        return _AR_BROKEN_PLURALS[w]
    for pref in _AR_PREFIXES:
        if w.startswith(pref) and len(w) - len(pref) >= 3:
            w = w[len(pref) :]
            break
    if len(w) >= 4 and w[0] in ("و", "ف", "ب", "ك", "ل") and w[1:] in _AR_BROKEN_PLURALS:
        return _AR_BROKEN_PLURALS[w[1:]]
    if w in _AR_BROKEN_PLURALS:
        return _AR_BROKEN_PLURALS[w]
    for suf in _AR_SUFFIXES:
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            w = w[: -len(suf)]
            break
    return _AR_BROKEN_PLURALS.get(w, w)


def _stem_english(word: str) -> str:
    w = word.lower()
    if len(w) <= 3:
        return w
    for suf in _EN_SUFFIXES:
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            if suf == "ies":
                return w[:-3] + "y"
            return w[: -len(suf)]
    return w


def _stem_token(token: str) -> str:
    if any(_is_arabic_char(ch) for ch in token):
        return _stem_arabic(token)
    return _stem_english(token)


# Cross-lingual semantic concept clusters mapping bilingual terms/stems -> concept IDs
_CONCEPT_CLUSTERS: dict[str, tuple[str, ...]] = {
    "concept:ai_ml": (
        "ai",
        "artificial",
        "intelligence",
        "machine",
        "learning",
        "deep",
        "neural",
        "network",
        "model",
        "llm",
        "nlp",
        "transformer",
        "classification",
        "classify",
        "clustering",
        "cluster",
        "training",
        "inference",
        "ذكاء",
        "اصطناعي",
        "تعلم",
        "اله",
        "آله",
        "عصبي",
        "شبكه",
        "نموذج",
        "نماذج",
        "تصنيف",
        "تجميع",
        "استدلال",
        "خوارزميه",
    ),
    "concept:finance_invoice": (
        "invoice",
        "billing",
        "bill",
        "payment",
        "paid",
        "revenue",
        "profit",
        "tax",
        "vat",
        "budget",
        "cost",
        "price",
        "amount",
        "money",
        "financial",
        "finance",
        "accounting",
        "bank",
        "currency",
        "dollar",
        "sar",
        "usd",
        "فاتوره",
        "فواتير",
        "دفع",
        "مدفوعات",
        "سداد",
        "ايراد",
        "ايرادات",
        "ربح",
        "ارباح",
        "ضريبه",
        "ضرائب",
        "ميزانيه",
        "تكلفه",
        "سعر",
        "مبلغ",
        "مالي",
        "ماليه",
        "محاسبه",
        "بنك",
        "مصرف",
        "ريال",
        "دولار",
        "جنيه",
    ),
    "concept:security_network": (
        "security",
        "cybersecurity",
        "cyber",
        "firewall",
        "ssrf",
        "egress",
        "encryption",
        "auth",
        "authentication",
        "token",
        "vulnerability",
        "attack",
        "injection",
        "guardrail",
        "sandbox",
        "isolation",
        "امن",
        "امان",
        "حمايه",
        "سيبراني",
        "جدار",
        "تشفير",
        "مصادقه",
        "رمز",
        "ثغره",
        "هجوم",
        "حقن",
        "عزل",
        "شبكه",
    ),
    "concept:analytics_forecast": (
        "forecast",
        "forecasting",
        "prediction",
        "predict",
        "trend",
        "regression",
        "timeseries",
        "series",
        "anomaly",
        "outlier",
        "statistics",
        "metric",
        "analytics",
        "analysis",
        "توقع",
        "توقعات",
        "تنبؤ",
        "اتجاه",
        "انحدار",
        "سلسله",
        "زمنيه",
        "شذوذ",
        "شاذ",
        "احصاء",
        "تحليل",
        "تحليلات",
    ),
    "concept:database_storage": (
        "database",
        "sql",
        "postgres",
        "redis",
        "index",
        "indexing",
        "search",
        "query",
        "vector",
        "storage",
        "cache",
        "table",
        "document",
        "قاعده",
        "بيانات",
        "فهرس",
        "فهرسه",
        "بحث",
        "استعلام",
        "تخزين",
        "جدول",
        "وثيقه",
        "مستند",
    ),
    "concept:browser_web": (
        "browser",
        "playwright",
        "chromium",
        "web",
        "html",
        "dom",
        "selector",
        "click",
        "navigate",
        "page",
        "screenshot",
        "http",
        "url",
        "متصفح",
        "تصفح",
        "ويب",
        "صفحه",
        "رابط",
        "نقر",
        "لقطه",
        "شاشه",
    ),
    "concept:vision_ocr": (
        "vision",
        "image",
        "photo",
        "picture",
        "png",
        "jpeg",
        "ocr",
        "pdf",
        "scan",
        "extract",
        "pixel",
        "resolution",
        "رؤيه",
        "صوره",
        "صور",
        "مسح",
        "ضوئي",
        "استخراج",
        "نص",
        "دقه",
        "بصري",
    ),
    "concept:audio_speech": (
        "audio",
        "speech",
        "voice",
        "sound",
        "wave",
        "wav",
        "transcribe",
        "transcription",
        "microphone",
        "whisper",
        "acoustic",
        "صوت",
        "صوتي",
        "كلام",
        "نطق",
        "تفريغ",
        "تسجيل",
        "موجه",
    ),
    "concept:cloud_infra": (
        "cloud",
        "server",
        "cluster",
        "kubernetes",
        "docker",
        "container",
        "cpu",
        "gpu",
        "memory",
        "deployment",
        "latency",
        "سحابه",
        "سحابي",
        "خادم",
        "خوادم",
        "حاويه",
        "معالج",
        "ذاكره",
        "نشر",
        "تشغيل",
    ),
    "concept:ecommerce_sales": (
        "sales",
        "customer",
        "client",
        "order",
        "product",
        "store",
        "market",
        "growth",
        "مبيعات",
        "بيع",
        "عميل",
        "عملاء",
        "طلب",
        "منتج",
        "متجر",
        "سوق",
        "نمو",
    ),
}

_STEM_TO_CONCEPTS: dict[str, set[str]] = {}
for _cid, _words in _CONCEPT_CLUSTERS.items():
    for _w in _words:
        _norm = normalise_arabic(_w.lower())
        _st = _stem_token(_norm)
        _STEM_TO_CONCEPTS.setdefault(_norm, set()).add(_cid)
        _STEM_TO_CONCEPTS.setdefault(_st, set()).add(_cid)


_TOKEN_RE = re.compile(r"[\u0600-\u06FFa-zA-Z0-9]{2,}")


def _tokenize(text: str) -> list[str]:
    norm = normalise_arabic(text.lower())
    return _TOKEN_RE.findall(norm)


def _extract_concepts(tokens: Sequence[str]) -> set[str]:
    concepts: set[str] = set()
    for tok in tokens:
        if tok in _STEM_TO_CONCEPTS:
            concepts.update(_STEM_TO_CONCEPTS[tok])
        stem = _stem_token(tok)
        if stem in _STEM_TO_CONCEPTS:
            concepts.update(_STEM_TO_CONCEPTS[stem])
    return concepts


def _char_ngrams(text: str, ns: tuple[int, ...] = (3, 4)) -> Counter[str]:
    norm = normalise_arabic(text.lower())
    norm = re.sub(r"[^\u0600-\u06FFa-z0-9 ]+", " ", norm)
    norm = _MULTI_SPACE_RE.sub(" ", norm).strip()
    if not norm:
        return Counter()
    padded = f" {norm} "
    counts: Counter[str] = Counter()
    for n in ns:
        if len(padded) >= n:
            for i in range(len(padded) - n + 1):
                counts[padded[i : i + n]] += 1
    return counts


def _counter_cosine(a: Counter[str], b: Counter[str]) -> float:
    if not a or not b:
        return 0.0
    common = set(a) & set(b)
    if not common:
        return 0.0
    dot = sum(a[k] * b[k] for k in common)
    norm_a = math.sqrt(sum(v * v for v in a.values()))
    norm_b = math.sqrt(sum(v * v for v in b.values()))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


# ---------------------------------------------------------------------------
# 3. Entity extraction patterns & gazetteers (Arabic + English)
# ---------------------------------------------------------------------------

_DATE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b\d{4}-\d{1,2}-\d{1,2}\b"),
    re.compile(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b"),
    re.compile(
        r"\b\d{1,2}\s+(?:January|February|March|April|May|June|July|August|"
        r"September|October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
        r"(?:,?\s+\d{4})?\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:January|February|March|April|May|June|July|August|"
        r"September|October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
        r"\s+\d{1,2}(?:,?\s+\d{4})?\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b\d{1,2}\s+(?:يناير|فبراير|مارس|أبريل|ابريل|مايو|يونيو|يوليو|"
        r"أغسطس|اغسطس|سبتمبر|أكتوبر|اكتوبر|نوفمبر|ديسمبر|"
        r"محرم|صفر|ربيع|جمادى|جمادي|رجب|شعبان|رمضان|شوال)(?:\s+\d{4})?\b"
    ),
)

_MONEY_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"(?:\$|€|£|¥)\s*\d[\d,]*(?:\.\d+)?(?:\s*(?:million|billion|thousand|k|m|b))?\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:USD|EUR|GBP|SAR|EGP|AED|KWD|QAR)\s*\d[\d,]*(?:\.\d+)?\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b\d[\d,]*(?:\.\d+)?\s*(?:(?:million|billion|thousand|ألف|الف|مليون|مليار)\s+)?"
        r"(?:USD|EUR|GBP|SAR|EGP|AED|KWD|QAR|dollars?|euros?|pounds?|"
        r"ريال(?:\s+سعودي)?|جنيه(?:\s+مصري)?|درهم(?:\s+اماراتي|\s+إماراتي)?|"
        r"دينار|دولار(?:\s+امريكي|\s+أمريكي)?)\b",
        re.IGNORECASE,
    ),
)

_PERCENT_RE = re.compile(
    r"\b\d+(?:\.\d+)?\s*(?:%|percent|بالمائة|بالمئة|بالمائه|بالمئه|في المائة|في المئة)",
    re.IGNORECASE,
)

_AR_LOCATIONS: tuple[str, ...] = (
    "القاهرة",
    "القاهره",
    "الرياض",
    "دبي",
    "أبوظبي",
    "ابوظبي",
    "جدة",
    "جده",
    "مكة",
    "مكه",
    "المدينة",
    "الإسكندرية",
    "الاسكندرية",
    "الاسكندريه",
    "الدوحة",
    "الدوحه",
    "الكويت",
    "عمان",
    "بيروت",
    "بغداد",
    "دمشق",
    "الخرطوم",
    "تونس",
    "الجزائر",
    "الرباط",
    "الدار البيضاء",
    "مسقط",
    "المنامة",
    "المنامه",
    "مصر",
    "السعودية",
    "السعوديه",
    "الإمارات",
    "الامارات",
    "قطر",
    "الأردن",
    "الاردن",
    "لبنان",
    "العراق",
    "سوريا",
    "المغرب",
    "لندن",
    "باريس",
    "نيويورك",
    "واشنطن",
    "طوكيو",
    "برلين",
    "سان فرانسيسكو",
)

_EN_LOCATIONS: tuple[str, ...] = (
    "Cairo",
    "Riyadh",
    "Dubai",
    "Abu Dhabi",
    "Jeddah",
    "Alexandria",
    "Doha",
    "Kuwait",
    "Amman",
    "Beirut",
    "Baghdad",
    "Damascus",
    "Tunis",
    "Algiers",
    "Rabat",
    "Casablanca",
    "Muscat",
    "Manama",
    "Egypt",
    "Saudi Arabia",
    "United Arab Emirates",
    "UAE",
    "Qatar",
    "Jordan",
    "Lebanon",
    "Iraq",
    "Morocco",
    "London",
    "Paris",
    "New York",
    "Washington",
    "Tokyo",
    "Berlin",
    "San Francisco",
    "Silicon Valley",
    "United States",
    "United Kingdom",
    "Germany",
    "France",
)

_AR_ORG_PREFIX_RE = re.compile(
    r"\b(?:شركة|شركه|مؤسسة|مؤسسه|بنك|مصرف|جامعة|جامعه|وزارة|وزاره|هيئة|هيئه|"
    r"منظمة|منظمه|معهد|مجموعة|مجموعه|وكالة|وكاله)"
    r"\s+[\u0600-\u06FF]+(?:\s+[\u0600-\u06FF]+){0,2}"
)

_EN_ORG_SUFFIX_RE = re.compile(
    r"\b[A-Z][A-Za-z0-9&.-]*(?:\s+[A-Z][A-Za-z0-9&.-]*){0,3}\s+"
    r"(?:Inc\.?|Corp\.?|Corporation|Ltd\.?|LLC|PLC|Group|Bank|University|"
    r"Ministry|Foundation|Technologies|Systems|Labs|Company|Co\.?|Holdings)\b"
)

_KNOWN_ORGS: tuple[str, ...] = (
    "OpenAI",
    "Microsoft",
    "Google",
    "Apple",
    "Amazon",
    "Meta",
    "DeepSeek",
    "Qwen",
    "Anthropic",
    "Tesla",
    "NVIDIA",
    "IBM",
    "Oracle",
    "Aramco",
    "SABIC",
    "أرامكو",
    "ارامكو",
    "سابك",
    "مايكروسوفت",
    "جوجل",
    "أمازون",
    "امازون",
)

_AR_PERSON_PREFIX_RE = re.compile(
    r"\b(?:المهندس|الدكتور|الأستاذ|الاستاذ|السيد|السيدة|السيده|الشيخ|"
    r"الأمير|الامير|الرئيس|الوزير|المستشار)"
    r"\s+[\u0600-\u06FF]+(?:\s+[\u0600-\u06FF]+){0,2}"
)

_AR_FIRST_NAMES = (
    "أحمد|احمد|محمد|علي|عمر|خالد|سعيد|يوسف|إبراهيم|ابراهيم|حسن|حسين|"
    "عبدالله|عبد الله|عبدالرحمن|عبد الرحمن|فاطمة|فاطمه|سارة|ساره|مريم|"
    "نور|ليلى|ليلي|زينب|سلمى|سلمي|طارق|كريم|مصطفى|مصطفي|محمود|ياسر|"
    "عادل|تامر|حسام|وليد|رامي|سلمان|فيصل|نواف|ماجد|سلطان|هند|منى|مني"
)
_AR_PERSON_NAME_RE = re.compile(
    rf"\b(?:{_AR_FIRST_NAMES})(?:\s+(?:بن\s+)?[\u0600-\u06FF]{{3,}}){{1,2}}\b"
)
_AR_SINGLE_NAME_RE = re.compile(rf"\b(?:{_AR_FIRST_NAMES})\b")

_EN_PERSON_TITLE_RE = re.compile(
    r"\b(?:Mr\.?|Mrs\.?|Ms\.?|Dr\.?|Prof\.?|Eng\.?)\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2}\b"
)
_EN_PERSON_TWO_WORD_RE = re.compile(r"\b([A-Z][a-z]{2,}\s+[A-Z][a-z]{2,})\b")
_EN_NON_PERSON_WORDS: frozenset[str] = frozenset(
    {
        "The",
        "This",
        "That",
        "These",
        "Those",
        "When",
        "Where",
        "While",
        "After",
        "Before",
        "During",
        "Under",
        "Over",
        "Between",
        "Total",
        "Amount",
        "Invoice",
        "Number",
        "Date",
        "Report",
        "Summary",
        "System",
        "Platform",
        "Local",
        "Engine",
        "Machine",
        "Learning",
        "Artificial",
        "Intelligence",
        "United",
        "States",
        "Kingdom",
        "Saudi",
        "Arabia",
        "New",
        "York",
        "San",
        "Francisco",
        "Silicon",
        "Valley",
        "Abu",
        "Dhabi",
        "January",
        "February",
        "March",
        "April",
        "May",
        "June",
        "July",
        "August",
        "September",
        "October",
        "November",
        "December",
    }
)


def _has_module(module_name: str) -> bool:
    try:
        return importlib.util.find_spec(module_name) is not None
    except (ImportError, ValueError):
        return False


def _shannon_entropy(data: bytes) -> float:
    if not data:
        return 0.0
    n = len(data)
    counts = Counter(data)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def _decode_b64(raw_b64: str) -> bytes:
    if not raw_b64 or not raw_b64.strip():
        raise ValueError("base64 payload must not be empty")
    cleaned = raw_b64.strip()
    if "," in cleaned and cleaned.lower().startswith("data:"):
        cleaned = cleaned.split(",", 1)[1].strip()
    try:
        data = base64.b64decode(cleaned, validate=False)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"invalid base64 payload: {exc}") from exc
    if not data:
        raise ValueError("decoded base64 payload is empty")
    return data


# ---------------------------------------------------------------------------
# 4. PNG / PDF binary helpers
# ---------------------------------------------------------------------------


def _parse_png_chunks(data: bytes) -> dict[str, Any]:
    if not data.startswith(b"\x89PNG\r\n\x1a\n") or len(data) < 24:
        raise ValueError("not a valid PNG file")
    pos = 8
    width = 0
    height = 0
    bit_depth = 8
    color_type = 2
    channels = 3
    text_entries: dict[str, str] = {}
    text_chunks: list[dict[str, str]] = []

    color_channels_map = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}

    while pos + 8 <= len(data):
        length = struct.unpack(">I", data[pos : pos + 4])[0]
        chunk_type = data[pos + 4 : pos + 8].decode("latin1", errors="replace")
        chunk_start = pos + 8
        chunk_end = chunk_start + length
        if chunk_end > len(data):
            break
        chunk_data = data[chunk_start:chunk_end]
        pos = chunk_end + 4  # skip 4-byte CRC

        if chunk_type == "IHDR" and len(chunk_data) >= 13:
            width, height = struct.unpack(">II", chunk_data[:8])
            bit_depth = chunk_data[8]
            color_type = chunk_data[9]
            channels = color_channels_map.get(color_type, 3)
        elif chunk_type == "tEXt":
            if b"\x00" in chunk_data:
                key_b, val_b = chunk_data.split(b"\x00", 1)
                key = key_b.decode("latin1", errors="ignore")
                val = val_b.decode("utf-8", errors="replace")
            else:
                key = "text"
                val = chunk_data.decode("utf-8", errors="replace")
            text_entries[key] = val
            text_chunks.append({"chunk": "tEXt", "key": key, "text": val})
        elif chunk_type == "zTXt":
            if b"\x00" in chunk_data:
                key_b, rest = chunk_data.split(b"\x00", 1)
                key = key_b.decode("latin1", errors="ignore")
                compressed = rest[1:] if len(rest) > 1 else b""
                try:
                    val = zlib.decompress(compressed).decode("utf-8", errors="replace")
                except zlib.error:
                    val = ""
                if val:
                    text_entries[key] = val
                    text_chunks.append({"chunk": "zTXt", "key": key, "text": val})
        elif chunk_type == "iTXt":
            parts = chunk_data.split(b"\x00", 5)
            if len(parts) >= 2:
                key = parts[0].decode("latin1", errors="ignore")
                rest = chunk_data[len(parts[0]) + 1 :]
                if len(rest) >= 2:
                    comp_flag = rest[0]
                    after_flags = rest[2:]
                    subparts = after_flags.split(b"\x00", 2)
                    payload = subparts[-1] if subparts else b""
                    if comp_flag == 1:
                        try:
                            val = zlib.decompress(payload).decode("utf-8", errors="replace")
                        except zlib.error:
                            val = ""
                    else:
                        val = payload.decode("utf-8", errors="replace")
                    if val:
                        text_entries[key] = val
                        text_chunks.append({"chunk": "iTXt", "key": key, "text": val})
        elif chunk_type == "IEND":
            break

    return {
        "width": width,
        "height": height,
        "bit_depth": bit_depth,
        "color_type": color_type,
        "channels": channels,
        "text_entries": text_entries,
        "text_chunks": text_chunks,
    }


def _unescape_pdf_string(raw: bytes) -> str:
    out = bytearray()
    i = 0
    n = len(raw)
    while i < n:
        b = raw[i]
        if b == 0x5C and i + 1 < n:  # backslash
            nxt = raw[i + 1]
            if nxt in (ord("n"), ord("r"), ord("t")):
                out.append({ord("n"): 10, ord("r"): 13, ord("t"): 9}[nxt])
                i += 2
            elif nxt in (ord("("), ord(")"), ord("\\")):
                out.append(nxt)
                i += 2
            elif ord("0") <= nxt <= ord("7"):
                oct_digits = chr(nxt)
                i += 2
                for _ in range(2):
                    if i < n and ord("0") <= raw[i] <= ord("7"):
                        oct_digits += chr(raw[i])
                        i += 1
                    else:
                        break
                out.append(int(oct_digits, 8) & 0xFF)
            else:
                out.append(nxt)
                i += 2
        else:
            out.append(b)
            i += 1
    if out.startswith(b"\xfe\xff"):
        return bytes(out[2:]).decode("utf-16-be", errors="replace")
    try:
        return bytes(out).decode("utf-8")
    except UnicodeDecodeError:
        return bytes(out).decode("latin1", errors="replace")


_PDF_STREAM_RE = re.compile(rb"stream\r?\n(.*?)\r?\nendstream", re.DOTALL)
_PDF_TJ_SINGLE_RE = re.compile(rb"\((?:\\.|[^\\()])*\)\s*Tj")
_PDF_TJ_ARRAY_RE = re.compile(rb"\[(.*?)\]\s*TJ", re.DOTALL)
_PDF_LITERAL_RE = re.compile(rb"\(((?:\\.|[^\\()])*)\)")


def _extract_pdf_text(data: bytes) -> str:
    buffers: list[bytes] = [data]
    for match in _PDF_STREAM_RE.finditer(data):
        raw_stream = match.group(1)
        buffers.append(raw_stream)
        for wbits in (zlib.MAX_WBITS, -zlib.MAX_WBITS):
            try:
                decompressed = zlib.decompress(raw_stream, wbits)
                buffers.append(decompressed)
                break
            except zlib.error:
                continue

    fragments: list[str] = []
    seen: set[str] = set()
    for buf in buffers:
        # Process Tj and TJ in order of appearance in buf
        token_re = re.compile(rb"(\((?:\\.|[^\\()])*\)\s*Tj|\[.*?\]\s*TJ)", re.DOTALL)
        buf_parts: list[str] = []
        for m in token_re.finditer(buf):
            segment = m.group(1)
            if segment.endswith(b"TJ"):
                pieces = [
                    _unescape_pdf_string(lit.group(1)) for lit in _PDF_LITERAL_RE.finditer(segment)
                ]
                combined = "".join(pieces).strip()
                if combined:
                    buf_parts.append(combined)
            else:
                lit = _PDF_LITERAL_RE.search(segment)
                if lit:
                    piece = _unescape_pdf_string(lit.group(1)).strip()
                    if piece:
                        buf_parts.append(piece)
        if buf_parts:
            line = " ".join(buf_parts)
            if line not in seen:
                seen.add(line)
                fragments.append(line)

    return "\n".join(fragments).strip()


# ---------------------------------------------------------------------------
# 5. LocalAIEngine implementation
# ---------------------------------------------------------------------------


class LocalAIEngine:
    """Self-contained CPU AI engine requiring zero external network or API keys.

    ``embedding_backend`` (ADR-0019) is the one optional addition: ``None`` keeps
    every path in this class byte-for-byte the sparse search it always was, and a
    backend adds a dense arm to :meth:`semantic_search`. The backend is *injected*,
    never discovered, so ``capabilities()`` can only report a dense arm that somebody
    actually wired up.
    """

    #: Weighted fusion, not rank fusion: both arms here land in ``[0, 1]``, so the
    #: blended number keeps the meaning that ``MemoryRetriever.min_score`` and
    #: ``SkillRetriever.min_score`` already threshold on. ``os_know`` fuses by rank
    #: instead, because its dense arm comes from *stored* vectors (ADR-0019).
    DEFAULT_SPARSE_WEIGHT = 0.6
    DEFAULT_DENSE_WEIGHT = 0.4

    def __init__(
        self,
        embedding_backend: Any | None = None,
        *,
        sparse_weight: float | None = None,
        dense_weight: float | None = None,
    ) -> None:
        self._indices: dict[str, list[dict[str, Any]]] = {"default": []}
        self.embed_backend = embedding_backend
        self.sparse_weight = (
            self.DEFAULT_SPARSE_WEIGHT if sparse_weight is None else float(sparse_weight)
        )
        self.dense_weight = (
            self.DEFAULT_DENSE_WEIGHT if dense_weight is None else float(dense_weight)
        )
        self._last_dense_error = ""

    # --- embeddings (ADR-0019) ------------------------------------------------
    def embedding_info(self) -> dict[str, Any]:
        """What dense retrieval will *actually* do — never what is merely installed."""
        backend = self.embed_backend
        if backend is None:
            return {"backend": None, "dense_retrieval": False, "dim": 0, "model": ""}
        info = getattr(backend, "info", None)
        if info is not None and hasattr(info, "as_dict"):
            return info.as_dict()
        return {
            "backend": getattr(backend, "name", "unknown"),
            "dense_retrieval": True,
            "dim": int(getattr(backend, "dim", 0) or 0),
            "model": "",
        }

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Dense vectors for ``texts``; raises when no backend was wired in.

        Public because the stores index vectors at write time — a chunk that is
        only ever embedded at query time makes every search pay for the whole
        corpus.
        """
        from os_tools.embedding import EmbeddingUnavailable

        if self.embed_backend is None:
            raise EmbeddingUnavailable(
                "none",
                "no embedding backend is configured (set CELA_EMBED_BACKEND=local|model|provider)",
            )
        return self.embed_backend.embed(list(texts))

    def _dense_scores(self, query: str, bodies: Sequence[str]) -> list[float] | None:
        """Cosine of the query against every candidate, or ``None`` to stay sparse.

        A backend failure degrades to the sparse ranking *and reports itself*: an
        embedding outage must not look like a corpus with no answers, and it must
        not look like a successful hybrid search either.
        """
        if self.embed_backend is None or not bodies:
            return None
        try:
            vectors = self.embed_backend.embed([query, *bodies])
        except Exception as exc:  # any backend outage: degrade, report, never crash
            self._last_dense_error = f"{type(exc).__name__}: {exc}"
            return None
        if len(vectors) != len(bodies) + 1:
            self._last_dense_error = (
                f"backend returned {len(vectors)} vectors for {len(bodies) + 1} inputs"
            )
            return None
        self._last_dense_error = ""
        from os_core.vectors import cosine

        q_vec = vectors[0]
        return [max(0.0, round(cosine(q_vec, vec), 6)) for vec in vectors[1:]]

    # --- 5.1 Capabilities ---------------------------------------------------

    def capabilities(self) -> dict[str, Any]:
        """Return local execution mode and optional dependency availability."""
        optional_libs = {
            "scikit_learn": _has_module("sklearn"),
            "spacy": _has_module("spacy"),
            "sentence_transformers": _has_module("sentence_transformers"),
            "faiss": _has_module("faiss"),
            "opencv": _has_module("cv2"),
            "paddleocr": _has_module("paddleocr"),
            "faster_whisper": _has_module("faster_whisper"),
        }
        return {
            "mode": "local-cpu",
            "external_network_required": False,
            "per_request_cost": 0.0,
            "offline": True,
            "languages": ["ar", "en", "mixed"],
            "tasks": [
                "nlp_entities",
                "semantic_search",
                "ml_classify",
                "ml_forecast",
                "ml_cluster",
                "ml_anomaly",
                "vision_analyze",
                "document_ocr",
                "audio_transcribe",
            ],
            "optional_libraries": optional_libs,
            "libraries": optional_libs,
            # A wheel being importable is not a capability. These two blocks are
            # deliberately separate: ``optional_libraries`` answers "is it
            # installed", ``embedding`` answers "will retrieval use a vector".
            # Conflating them is the exact report this module used to give.
            "embedding": self.embedding_info(),
            "dense_retrieval": self.embed_backend is not None,
            "retrieval": {
                "mode": "hybrid" if self.embed_backend is not None else "sparse",
                "fusion": "weighted" if self.embed_backend is not None else "none",
                "weights": {"sparse": self.sparse_weight, "dense": self.dense_weight},
            },
            **optional_libs,
            "namespaces": {ns: len(docs) for ns, docs in self._indices.items()},
        }

    # --- 5.2 NLP Entity & Keyword Extraction --------------------------------

    def extract_entities(self, text: str, language: str = "auto") -> dict[str, Any]:
        """Normalise Arabic text/digits, extract keywords and 9 entity categories."""
        raw_text = text or ""
        norm_digits_text = raw_text.translate(_INDIC_DIGITS_MAP)
        norm_text = normalise_arabic(raw_text)
        detected_lang = detect_language(raw_text) if language == "auto" else language

        by_type: dict[str, list[str]] = {
            "PERSON": [],
            "LOCATION": [],
            "ORG": [],
            "DATE": [],
            "MONEY": [],
            "PERCENT": [],
            "EMAIL": [],
            "URL": [],
            "PHONE": [],
        }
        entities: list[dict[str, str]] = []

        def _add_entity(etype: str, val: str) -> None:
            cleaned = val.strip(" ,.;:()[]{}،؛\"'")
            if not cleaned:
                return
            if cleaned not in by_type[etype]:
                by_type[etype].append(cleaned)
                entities.append(
                    {
                        "type": etype,
                        "label": etype,
                        "text": cleaned,
                        "value": cleaned,
                    }
                )

        # 1) URL
        for m in _URL_RE.finditer(norm_digits_text):
            _add_entity("URL", m.group(0))

        # Strip URLs before downstream regexes so URL paths don't trigger false dates/phones
        text_no_url = _URL_RE.sub(" ", norm_digits_text)

        # 2) EMAIL
        for m in _EMAIL_RE.finditer(text_no_url):
            _add_entity("EMAIL", m.group(0))
        text_no_email = _EMAIL_RE.sub(" ", text_no_url)

        # 3) DATE
        for pat in _DATE_PATTERNS:
            for m in pat.finditer(text_no_email):
                _add_entity("DATE", m.group(0))

        # 4) MONEY
        for pat in _MONEY_PATTERNS:
            for m in pat.finditer(text_no_email):
                _add_entity("MONEY", m.group(0))

        # 5) PERCENT
        for m in _PERCENT_RE.finditer(text_no_email):
            _add_entity("PERCENT", m.group(0))

        # 6) PHONE (avoid overlapping with already extracted DATEs)
        date_set = set(by_type["DATE"])
        for m in _PHONE_RE.finditer(text_no_email):
            candidate = m.group(0).strip()
            if candidate not in date_set and not re.fullmatch(r"\d{4}-\d{1,2}-\d{1,2}", candidate):
                _add_entity("PHONE", candidate)

        # 7) ORG
        for m in _AR_ORG_PREFIX_RE.finditer(text_no_email):
            # Stop at common Arabic prepositions/conjunctions if captured
            phrase = re.split(
                r"\s+(?:في|من|إلى|الى|على|عن|مع|بمبلغ|بقيمة|بتاريخ|لعام|بـ)\b",
                m.group(0),
            )[0]
            _add_entity("ORG", phrase)
        for m in _EN_ORG_SUFFIX_RE.finditer(text_no_email):
            _add_entity("ORG", m.group(0))
        for org in _KNOWN_ORGS:
            org_pat = rf"(?<![\w\u0600-\u06FF]){re.escape(org)}(?![\w\u0600-\u06FF])"
            if re.search(org_pat, text_no_email, re.IGNORECASE) and not any(
                org.lower() in existing.lower() for existing in by_type["ORG"]
            ):
                _add_entity("ORG", org)

        # 8) LOCATION
        for loc in _AR_LOCATIONS:
            # Match with optional Arabic prefix (في / بـ / من / إلى)
            pat = re.compile(
                rf"(?<![\u0600-\u06FF])(?:ب|ل|في\s+)?({re.escape(loc)})(?![\u0600-\u06FF])"
            )
            for m in pat.finditer(text_no_email):
                _add_entity("LOCATION", m.group(1))
        for loc in _EN_LOCATIONS:
            pat = re.compile(rf"\b{re.escape(loc)}\b", re.IGNORECASE)
            for m in pat.finditer(text_no_email):
                _add_entity("LOCATION", m.group(0))

        # 9) PERSON
        already_used = set(by_type["ORG"]) | set(by_type["LOCATION"])
        for m in _AR_PERSON_PREFIX_RE.finditer(text_no_email):
            phrase = re.split(
                r"\s+(?:في|من|إلى|الى|على|عن|مع|فيما|حيث|الذي|التي|بشركة|لدى)\b",
                m.group(0),
            )[0]
            _add_entity("PERSON", phrase)
        for m in _AR_PERSON_NAME_RE.finditer(text_no_email):
            phrase = re.split(
                r"\s+(?:في|من|إلى|الى|على|عن|مع|حيث|الذي|التي|بشركة|لدى|مدير|رئيس)\b",
                m.group(0),
            )[0]
            if not any(phrase in p for p in by_type["PERSON"]):
                _add_entity("PERSON", phrase)
        if not by_type["PERSON"]:
            for m in _AR_SINGLE_NAME_RE.finditer(text_no_email):
                _add_entity("PERSON", m.group(0))
        for m in _EN_PERSON_TITLE_RE.finditer(text_no_email):
            _add_entity("PERSON", m.group(0))
        for m in _EN_PERSON_TWO_WORD_RE.finditer(text_no_email):
            cand = m.group(1)
            parts = cand.split()
            if (
                cand not in already_used
                and not any(cand in o for o in by_type["ORG"])
                and not any(cand in loc for loc in by_type["LOCATION"])
                and not any(p in _EN_NON_PERSON_WORDS for p in parts)
            ):
                _add_entity("PERSON", cand)

        # Keywords extraction
        tokens = _tokenize(text_no_email)
        keywords: list[str] = []
        seen_kw: set[str] = set()
        for tok in tokens:
            if tok.isdigit() or len(tok) < 3:
                continue
            if tok in _EN_STOPWORDS or tok in _AR_STOPWORDS:
                continue
            if tok not in seen_kw:
                seen_kw.add(tok)
                keywords.append(tok)

        return {
            "language": detected_lang,
            "normalised_text": norm_text,
            "normalized_text": norm_text,
            "entities": entities,
            "entities_by_type": by_type,
            "keywords": keywords[:25],
            "token_count": len(tokens),
        }

    # --- 5.3 Hybrid Semantic Search -----------------------------------------

    @staticmethod
    def _coerce_documents(
        documents: Sequence[str | dict[str, Any]],
        *,
        start_index: int = 0,
    ) -> list[dict[str, Any]]:
        coerced: list[dict[str, Any]] = []
        for idx, item in enumerate(documents, start=start_index):
            if isinstance(item, str):
                coerced.append(
                    {
                        "id": f"doc_{idx}",
                        "text": item,
                        "title": None,
                        "metadata": {},
                    }
                )
            elif isinstance(item, dict):
                doc_id = str(item.get("id", f"doc_{idx}"))
                doc_text = str(
                    item.get("text")
                    or item.get("content")
                    or item.get("body")
                    or item.get("title")
                    or ""
                )
                doc_title = item.get("title")
                meta = dict(item.get("metadata") or {})
                for k, v in item.items():
                    if k not in {"id", "text", "content", "body", "title", "metadata"}:
                        meta.setdefault(k, v)
                coerced.append(
                    {
                        "id": doc_id,
                        "text": doc_text,
                        "title": doc_title,
                        "metadata": meta,
                    }
                )
            else:
                raise ValueError(f"unsupported document type: {type(item).__name__}")
        return coerced

    def index_documents(
        self,
        documents: Sequence[str | dict[str, Any]],
        namespace: str = "default",
        clear: bool = False,
    ) -> dict[str, Any]:
        """Index *documents* into an in-memory *namespace*."""
        if clear or namespace not in self._indices:
            self._indices[namespace] = []
        existing = self._indices[namespace]
        coerced = self._coerce_documents(documents, start_index=len(existing))
        by_id = {d["id"]: i for i, d in enumerate(existing)}
        for doc in coerced:
            if doc["id"] in by_id:
                existing[by_id[doc["id"]]] = doc
            else:
                by_id[doc["id"]] = len(existing)
                existing.append(doc)
        return {
            "namespace": namespace,
            "indexed": len(coerced),
            "total_documents": len(existing),
            "count": len(existing),
        }

    def semantic_search(
        self,
        query: str,
        documents: Sequence[str | dict[str, Any]] | None = None,
        namespace: str = "default",
        top_k: int = 5,
        *,
        use_dense: bool = True,
    ) -> dict[str, Any]:
        """Hybrid search: light stemming + TF-IDF + char 3/4-grams + concept clusters.

        ``use_dense=False`` runs the sparse arm only even when a backend is wired.
        Layers that store vectors themselves (``os_know``, ``os_memory``) must pass it:
        otherwise the same documents get embedded once from the store and again here,
        which is a second bill on the ``provider`` backend and a second, differently
        weighted, ranking of the same evidence.
        """
        if top_k < 1:
            raise ValueError("top_k must be >= 1")
        candidates = (
            self._coerce_documents(documents)
            if documents is not None
            else list(self._indices.get(namespace, []))
        )
        if not candidates:
            return {
                "query": query,
                "namespace": namespace,
                "language": detect_language(query),
                "top_k": top_k,
                "total_candidates": 0,
                "results": [],
            }

        q_tokens = _tokenize(query)
        q_stems = [_stem_token(t) for t in q_tokens]
        q_concepts = _extract_concepts(q_tokens)
        q_ngrams = _char_ngrams(query)

        doc_stems_list: list[list[str]] = []
        doc_concepts_list: list[set[str]] = []
        doc_ngrams_list: list[Counter[str]] = []
        df: Counter[str] = Counter()

        for doc in candidates:
            full_text = f"{doc.get('title') or ''} {doc['text']}".strip()
            d_tokens = _tokenize(full_text)
            d_stems = [_stem_token(t) for t in d_tokens]
            d_concepts = _extract_concepts(d_tokens)
            d_ngrams = _char_ngrams(full_text)
            doc_stems_list.append(d_stems)
            doc_concepts_list.append(d_concepts)
            doc_ngrams_list.append(d_ngrams)
            for term in set(d_stems) | d_concepts:
                df[term] += 1

        n_docs = len(candidates)

        def _tfidf_vec(stems: Sequence[str], concepts: set[str]) -> dict[str, float]:
            tf = Counter(stems)
            for c in concepts:
                tf[c] += 1
            vec: dict[str, float] = {}
            for term, count in tf.items():
                idf = math.log((1.0 + n_docs) / (1.0 + df.get(term, 0))) + 1.0
                vec[term] = (1.0 + math.log(count)) * idf
            norm = math.sqrt(sum(v * v for v in vec.values()))
            if norm > 0:
                return {k: v / norm for k, v in vec.items()}
            return vec

        q_vec = _tfidf_vec(q_stems, q_concepts)

        scored: list[dict[str, Any]] = []
        for idx, doc in enumerate(candidates):
            d_stems = doc_stems_list[idx]
            d_concepts = doc_concepts_list[idx]
            d_ngrams = doc_ngrams_list[idx]
            d_vec = _tfidf_vec(d_stems, d_concepts)

            # 1) Stemmed + concept TF-IDF cosine similarity
            tfidf_sim = sum(q_vec[k] * d_vec.get(k, 0.0) for k in q_vec)

            # 2) Character 3/4-gram cosine similarity
            ngram_sim = _counter_cosine(q_ngrams, d_ngrams)

            # 3) Cross-lingual semantic concept overlap (Jaccard / query coverage)
            shared_concepts = sorted(q_concepts & d_concepts)
            if q_concepts and d_concepts:
                concept_coverage = len(shared_concepts) / len(q_concepts)
                concept_jaccard = len(shared_concepts) / len(q_concepts | d_concepts)
                concept_sim = 0.65 * concept_coverage + 0.35 * concept_jaccard
            else:
                concept_sim = 0.0

            # 4) Exact stem overlap ratio
            q_stem_set = set(q_stems)
            d_stem_set = set(d_stems)
            stem_overlap = len(q_stem_set & d_stem_set) / len(q_stem_set) if q_stem_set else 0.0

            hybrid_score = (
                0.40 * tfidf_sim + 0.20 * ngram_sim + 0.25 * concept_sim + 0.15 * stem_overlap
            )
            scored.append(
                {
                    "id": doc["id"],
                    "text": doc["text"],
                    "title": doc.get("title"),
                    "metadata": doc.get("metadata", {}),
                    "score": round(min(1.0, hybrid_score), 4),
                    "matched_concepts": shared_concepts,
                }
            )

        # Dense arm (ADR-0019). Bodies are built the same way the sparse arm sees
        # them, so a title that carries the answer is not silently dropped from
        # one of the two rankings.
        dense_scores: list[float] | None = None
        if self.embed_backend is not None and use_dense:
            dense_scores = self._dense_scores(
                query,
                [f"{doc.get('title') or ''} {doc['text']}".strip() for doc in candidates],
            )
        if dense_scores is not None:
            for idx, entry in enumerate(scored):
                entry["sparse_score"] = entry["score"]
                entry["dense_score"] = dense_scores[idx]
                entry["score"] = round(
                    min(
                        1.0,
                        self.sparse_weight * entry["sparse_score"]
                        + self.dense_weight * entry["dense_score"],
                    ),
                    4,
                )
        else:
            for entry in scored:
                entry["sparse_score"] = entry["score"]
                entry["dense_score"] = None

        # Tie-break on id: two documents can land on the same 4-dp score, and an
        # order that depends on insertion order is a number nobody can reproduce.
        scored.sort(key=lambda item: (-item["score"], str(item["id"])))
        return {
            "query": query,
            "namespace": namespace,
            "language": detect_language(query),
            "top_k": top_k,
            "total_candidates": len(candidates),
            "retrieval": {
                "mode": "hybrid" if dense_scores is not None else "sparse",
                "ad_hoc_dense": dense_scores is not None,
                "fusion": "weighted" if dense_scores is not None else "none",
                "backend": getattr(self.embed_backend, "name", None)
                if dense_scores is not None
                else None,
                "weights": {"sparse": self.sparse_weight, "dense": self.dense_weight},
                "degraded_reason": getattr(self, "_last_dense_error", ""),
            },
            "results": scored[:top_k],
        }

    # --- 5.4 Classical ML & Statistical Analysis ----------------------------

    def ml_analyze(self, task: str, **kwargs: Any) -> dict[str, Any]:
        """Run one of: ``classify``, ``forecast``, ``cluster``, ``anomaly``."""
        task_norm = (task or "").strip().lower()
        if task_norm == "classify":
            return self._ml_classify(**kwargs)
        if task_norm == "forecast":
            return self._ml_forecast(**kwargs)
        if task_norm == "cluster":
            return self._ml_cluster(**kwargs)
        if task_norm == "anomaly":
            return self._ml_anomaly(**kwargs)
        raise ValueError(
            f"unsupported ml_analyze task {task!r}; "
            "expected classify, forecast, cluster, or anomaly"
        )

    def _ml_classify(self, **kwargs: Any) -> dict[str, Any]:
        train_data = (
            kwargs.get("train_data")
            or kwargs.get("X_train")
            or kwargs.get("train_features")
            or kwargs.get("features")
        )
        train_labels = kwargs.get("train_labels") or kwargs.get("y_train") or kwargs.get("labels")
        test_data = (
            kwargs.get("test_data")
            or kwargs.get("X_test")
            or kwargs.get("inputs")
            or kwargs.get("samples")
            or kwargs.get("data")
        )
        k = int(kwargs.get("k") or kwargs.get("n_neighbors") or 3)

        if not train_data or not train_labels or test_data is None:
            raise ValueError("classify requires train_data, train_labels, and test_data")
        x_train = [[float(v) for v in row] for row in train_data]
        y_train = list(train_labels)
        if len(x_train) != len(y_train):
            raise ValueError("train_data and train_labels must have the same length")
        if not test_data:
            raise ValueError("test_data must not be empty")
        if isinstance(test_data[0], (int, float)):
            x_test = [[float(v) for v in test_data]]
        else:
            x_test = [[float(v) for v in row] for row in test_data]

        n_features = len(x_train[0])
        if n_features == 0 or any(len(r) != n_features for r in x_train + x_test):
            raise ValueError("all feature vectors must have identical non-zero dimension")

        # Standard z-score scaling per feature
        means: list[float] = []
        stds: list[float] = []
        for col in range(n_features):
            col_vals = [row[col] for row in x_train]
            mean_c = sum(col_vals) / len(col_vals)
            var_c = sum((v - mean_c) ** 2 for v in col_vals) / len(col_vals)
            std_c = math.sqrt(var_c)
            means.append(mean_c)
            stds.append(std_c if std_c > 1e-12 else 1.0)

        def _scale(row: list[float]) -> list[float]:
            return [(row[c] - means[c]) / stds[c] for c in range(n_features)]

        scaled_train = [_scale(r) for r in x_train]
        scaled_test = [_scale(r) for r in x_test]
        k_eff = max(1, min(k, len(scaled_train)))
        classes = sorted({str(lbl) for lbl in y_train})
        label_lookup = {str(lbl): lbl for lbl in y_train}

        predictions: list[Any] = []
        probabilities: list[dict[str, float]] = []

        for test_pt in scaled_test:
            dists: list[tuple[float, Any]] = []
            for train_pt, lbl in zip(scaled_train, y_train, strict=True):
                dist = math.sqrt(sum((a - b) ** 2 for a, b in zip(test_pt, train_pt, strict=True)))
                dists.append((dist, lbl))
            dists.sort(key=lambda pair: pair[0])
            nearest = dists[:k_eff]

            weights: dict[str, float] = dict.fromkeys(classes, 0.0)
            for dist, lbl in nearest:
                w = 1.0 / (dist + 1e-6)
                weights[str(lbl)] += w
            total_w = sum(weights.values()) or 1.0
            probs = {c: round(weights[c] / total_w, 4) for c in classes}
            best_class_key = max(classes, key=lambda c: weights[c])
            predictions.append(label_lookup[best_class_key])
            probabilities.append(probs)

        return {
            "task": "classify",
            "algorithm": "k-nn",
            "k": k_eff,
            "predictions": predictions,
            "probabilities": probabilities,
            "classes": [label_lookup[c] for c in classes],
        }

    def _ml_forecast(self, **kwargs: Any) -> dict[str, Any]:
        raw_series = kwargs.get("series") or kwargs.get("data") or kwargs.get("values")
        horizon = int(kwargs.get("horizon") or kwargs.get("steps") or 5)
        if not raw_series or len(raw_series) < 2:
            raise ValueError("forecast requires a series with at least 2 values")
        if horizon < 1:
            raise ValueError("horizon must be >= 1")

        y = [float(v) for v in raw_series]
        n = len(y)
        xs = [float(i) for i in range(n)]
        mean_x = sum(xs) / n
        mean_y = sum(y) / n

        ss_xx = sum((x - mean_x) ** 2 for x in xs)
        ss_xy = sum((x - mean_x) * (yv - mean_y) for x, yv in zip(xs, y, strict=True))
        slope = ss_xy / ss_xx if ss_xx > 0 else 0.0
        intercept = mean_y - slope * mean_x

        y_pred = [intercept + slope * x for x in xs]
        ss_tot = sum((yv - mean_y) ** 2 for yv in y)
        ss_res = sum((yv - yp) ** 2 for yv, yp in zip(y, y_pred, strict=True))
        r2 = 1.0 if ss_tot <= 1e-12 else max(0.0, min(1.0, 1.0 - (ss_res / ss_tot)))

        forecast_vals = [round(intercept + slope * (n + i), 6) for i in range(horizon)]
        if slope > 1e-6:
            trend = "increasing"
        elif slope < -1e-6:
            trend = "decreasing"
        else:
            trend = "stable"

        return {
            "task": "forecast",
            "algorithm": "linear_regression",
            "horizon": horizon,
            "forecast": forecast_vals,
            "slope": round(slope, 6),
            "intercept": round(intercept, 6),
            "r2_score": round(r2, 6),
            "trend": trend,
        }

    def _ml_cluster(self, **kwargs: Any) -> dict[str, Any]:
        raw_data = kwargs.get("data") or kwargs.get("points") or kwargs.get("series")
        n_clusters = int(kwargs.get("n_clusters") or kwargs.get("k") or 2)
        max_iter = int(kwargs.get("max_iter") or 100)

        if not raw_data:
            raise ValueError("cluster requires non-empty data")
        if isinstance(raw_data[0], (int, float)):
            points = [[float(v)] for v in raw_data]
        else:
            points = [[float(v) for v in row] for row in raw_data]

        n_points = len(points)
        if n_clusters < 1 or n_clusters > n_points:
            raise ValueError("n_clusters must be between 1 and len(data)")
        dim = len(points[0])
        if dim == 0 or any(len(p) != dim for p in points):
            raise ValueError("all data points must have the same non-zero dimension")

        def _sq_dist(a: list[float], b: list[float]) -> float:
            return sum((x - y) ** 2 for x, y in zip(a, b, strict=True))

        # Deterministic k-means++ seeding: start from lexicographically smallest point
        first_idx = min(range(n_points), key=lambda i: tuple(points[i]))
        centroids: list[list[float]] = [list(points[first_idx])]
        chosen_indices = {first_idx}

        while len(centroids) < n_clusters:
            best_idx = -1
            best_dist = -1.0
            for idx, pt in enumerate(points):
                if idx in chosen_indices:
                    continue
                d_min = min(_sq_dist(pt, c) for c in centroids)
                if d_min > best_dist:
                    best_dist = d_min
                    best_idx = idx
            if best_idx == -1:
                best_idx = next(i for i in range(n_points) if i not in chosen_indices)
            chosen_indices.add(best_idx)
            centroids.append(list(points[best_idx]))

        labels = [0] * n_points
        for _ in range(max_iter):
            new_labels: list[int] = []
            for pt in points:
                best_c = min(range(n_clusters), key=lambda c: _sq_dist(pt, centroids[c]))
                new_labels.append(best_c)
            if new_labels == labels and _ > 0:
                break
            labels = new_labels
            for c in range(n_clusters):
                members = [points[i] for i, lbl in enumerate(labels) if lbl == c]
                if members:
                    centroids[c] = [sum(m[d] for m in members) / len(members) for d in range(dim)]

        inertia = sum(_sq_dist(points[i], centroids[labels[i]]) for i in range(n_points))
        cluster_sizes = [labels.count(c) for c in range(n_clusters)]
        rounded_centroids = [[round(v, 6) for v in c] for c in centroids]

        return {
            "task": "cluster",
            "algorithm": "kmeans++",
            "n_clusters": n_clusters,
            "labels": labels,
            "centroids": rounded_centroids,
            "cluster_sizes": cluster_sizes,
            "inertia": round(inertia, 6),
        }

    def _ml_anomaly(self, **kwargs: Any) -> dict[str, Any]:
        raw = kwargs.get("series") if kwargs.get("series") is not None else kwargs.get("data")
        if raw is None:
            raw = kwargs.get("values")
        if not raw or len(raw) < 3:
            raise ValueError("anomaly requires series or data with at least 3 elements")
        z_threshold = float(kwargs.get("z_threshold") or kwargs.get("threshold") or 3.0)

        if isinstance(raw[0], (list, tuple)):
            dim = len(raw[0])
            pts = [[float(v) for v in row] for row in raw]
            med_vec = [statistics.median(p[d] for p in pts) for d in range(dim)]
            values = [math.sqrt(sum((p[d] - med_vec[d]) ** 2 for d in range(dim))) for p in pts]
        else:
            values = [float(v) for v in raw]

        med = statistics.median(values)
        abs_devs = [abs(v - med) for v in values]
        mad = statistics.median(abs_devs)
        mean_ad = sum(abs_devs) / len(abs_devs)

        scores: list[float] = []
        anomalies: list[dict[str, Any]] = []
        for idx, (raw_item, _val, dev) in enumerate(zip(raw, values, abs_devs, strict=True)):
            if mad > 1e-12:
                z = 0.6745 * dev / mad
            elif mean_ad > 1e-12:
                z = dev / (mean_ad * 1.253314)
            else:
                z = 0.0
            z_round = round(z, 4)
            scores.append(z_round)
            if z >= z_threshold:
                anomalies.append(
                    {
                        "index": idx,
                        "value": raw_item,
                        "z_score": z_round,
                        "score": z_round,
                    }
                )

        return {
            "task": "anomaly",
            "algorithm": "mad_z_score",
            "z_threshold": z_threshold,
            "median": round(med, 6),
            "mad": round(mad, 6),
            "scores": scores,
            "anomalies": anomalies,
            "anomaly_indices": [a["index"] for a in anomalies],
            "count": len(anomalies),
        }

    # --- 5.5 Binary Computer Vision Inspection ------------------------------

    def vision_analyze(self, image_b64: str) -> dict[str, Any]:
        """Inspect binary image headers (PNG/JPEG/GIF/BMP/PPM), hashes & entropy."""
        data = _decode_b64(image_b64)
        sha256_hex = hashlib.sha256(data).hexdigest()
        entropy = round(_shannon_entropy(data), 4)

        fmt = "UNKNOWN"
        mime_type = "application/octet-stream"
        width = 0
        height = 0
        channels = 0
        metadata: dict[str, Any] = {}
        text_chunks: list[dict[str, str]] = []
        embedded_text = ""

        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            fmt = "PNG"
            mime_type = "image/png"
            png_info = _parse_png_chunks(data)
            width = png_info["width"]
            height = png_info["height"]
            channels = png_info["channels"]
            metadata = dict(png_info["text_entries"])
            text_chunks = png_info["text_chunks"]
            embedded_text = "\n".join(png_info["text_entries"].values()).strip()
        elif data.startswith(b"\xff\xd8"):
            fmt = "JPEG"
            mime_type = "image/jpeg"
            pos = 2
            sof_markers = {
                0xC0,
                0xC1,
                0xC2,
                0xC3,
                0xC5,
                0xC6,
                0xC7,
                0xC9,
                0xCA,
                0xCB,
                0xCD,
                0xCE,
                0xCF,
            }
            while pos < len(data) - 1:
                if data[pos] != 0xFF:
                    pos += 1
                    continue
                while pos < len(data) and data[pos] == 0xFF:
                    pos += 1
                if pos >= len(data):
                    break
                marker = data[pos]
                pos += 1
                if marker in (0xD8, 0xD9) or (0xD0 <= marker <= 0xD7):
                    continue
                if pos + 2 > len(data):
                    break
                seg_len = struct.unpack(">H", data[pos : pos + 2])[0]
                if marker in sof_markers and pos + 8 <= len(data):
                    height, width = struct.unpack(">HH", data[pos + 3 : pos + 7])
                    channels = data[pos + 7]
                    break
                pos += seg_len
        elif data.startswith((b"GIF87a", b"GIF89a")) and len(data) >= 10:
            fmt = "GIF"
            mime_type = "image/gif"
            width, height = struct.unpack("<HH", data[6:10])
            channels = 3
        elif data.startswith(b"BM") and len(data) >= 26:
            fmt = "BMP"
            mime_type = "image/bmp"
            width, height_signed = struct.unpack("<ii", data[18:26])
            height = abs(height_signed)
            bpp = struct.unpack("<H", data[28:30])[0] if len(data) >= 30 else 24
            channels = max(1, bpp // 8)
        elif data[:2] in (b"P1", b"P2", b"P3", b"P4", b"P5", b"P6"):
            fmt = "PPM"
            mime_type = "image/x-portable-pixmap"
            magic = data[:2].decode("ascii")
            tokens: list[bytes] = []
            for line in data.splitlines():
                stripped = line.split(b"#", 1)[0].strip()
                if stripped:
                    tokens.extend(stripped.split())
                if len(tokens) >= 4:
                    break
            if len(tokens) >= 3:
                width = int(tokens[1])
                height = int(tokens[2])
            channels = 3 if magic in ("P3", "P6") else 1
        else:
            raise ValueError("unsupported or corrupted binary image format")

        return {
            "format": fmt,
            "mime_type": mime_type,
            "width": width,
            "height": height,
            "channels": channels,
            "aspect_ratio": round(width / height, 4) if width and height else 0.0,
            "size_bytes": len(data),
            "sha256": sha256_hex,
            "entropy_bits": entropy,
            "metadata": metadata,
            "text_chunks": text_chunks,
            "embedded_text": embedded_text,
        }

    # --- 5.6 Document OCR & Structured Field Extraction ---------------------

    def document_ocr(
        self,
        content_b64: str | None = None,
        text: str | None = None,
        extract_fields: bool = True,
    ) -> dict[str, Any]:
        """Extract text & structured fields from PDF, PNG, HTML/SVG, or plain text."""
        if not content_b64 and (text is None or not str(text).strip()):
            raise ValueError("document_ocr requires either content_b64 or text")

        doc_format = "TEXT"
        extracted_parts: list[str] = []

        if content_b64:
            raw = _decode_b64(content_b64)
            if raw.startswith(b"%PDF"):
                doc_format = "PDF"
                extracted_parts.append(_extract_pdf_text(raw))
            elif raw.startswith(b"\x89PNG\r\n\x1a\n"):
                doc_format = "PNG"
                png_info = _parse_png_chunks(raw)
                extracted_parts.append("\n".join(png_info["text_entries"].values()))
            else:
                decoded = raw.decode("utf-8", errors="replace")
                markup_re = r"<(?:!doctype\s+html|html|svg|body|div|p|text|table)\b"
                if re.search(markup_re, decoded, re.I):
                    doc_format = "SVG" if "<svg" in decoded.lower() else "HTML"
                    extracted_parts.append(self._strip_markup(decoded))
                else:
                    doc_format = "TEXT"
                    extracted_parts.append(decoded)

        if text is not None and str(text).strip():
            raw_text = str(text)
            markup_re = r"<(?:!doctype\s+html|html|svg|body|div|p|text|table)\b"
            if re.search(markup_re, raw_text, re.I):
                if doc_format == "TEXT":
                    doc_format = "SVG" if "<svg" in raw_text.lower() else "HTML"
                extracted_parts.append(self._strip_markup(raw_text))
            else:
                extracted_parts.append(raw_text)

        full_text = "\n".join(p.strip() for p in extracted_parts if p and p.strip()).strip()
        norm_text = normalise_arabic(full_text)
        lang = detect_language(full_text)

        fields: dict[str, list[str]] = {
            "dates": [],
            "amounts": [],
            "emails": [],
            "urls": [],
            "organizations": [],
            "persons": [],
            "locations": [],
            "invoice_numbers": [],
        }

        if extract_fields and full_text:
            ent = self.extract_entities(full_text)
            by_type = ent["entities_by_type"]
            fields = {
                "dates": by_type["DATE"],
                "amounts": by_type["MONEY"],
                "emails": by_type["EMAIL"],
                "urls": by_type["URL"],
                "organizations": by_type["ORG"],
                "persons": by_type["PERSON"],
                "locations": by_type["LOCATION"],
                "invoice_numbers": self._extract_invoice_numbers(full_text),
            }

        return {
            "format": doc_format,
            "text": full_text,
            "normalised_text": norm_text,
            "language": lang,
            "fields": fields,
            **fields,
        }

    @staticmethod
    def _strip_markup(markup: str) -> str:
        cleaned = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", " ", markup, flags=re.I | re.S)
        cleaned = re.sub(r"<[^>]+>", " ", cleaned)
        cleaned = html.unescape(cleaned)
        cleaned = re.sub(r"[ \t]+", " ", cleaned)
        cleaned = re.sub(r"\n\s*\n+", "\n", cleaned)
        return cleaned.strip()

    @staticmethod
    def _extract_invoice_numbers(text: str) -> list[str]:
        norm = text.translate(_INDIC_DIGITS_MAP)
        patterns = (
            re.compile(r"\b(INV-[A-Z0-9-]+)\b", re.IGNORECASE),
            re.compile(
                r"(?:Invoice(?:\s+No\.?|\s+Number|\s+ID|\s*#)?|Bill\s+No\.?)"
                r"\s*[:#-]?\s*([A-Z0-9][A-Z0-9/-]{2,})\b",
                re.IGNORECASE,
            ),
            re.compile(
                r"(?:رقم\s+الفاتورة|رقم\s+الفاتوره|فاتورة\s+رقم|فاتوره\s+رقم|الفاتورة\s+رقم)"
                r"\s*[:#-]?\s*([A-Za-z0-9][A-Za-z0-9/-]{1,})"
            ),
        )
        found: list[str] = []
        for pat in patterns:
            for m in pat.finditer(norm):
                val = m.group(1).strip()
                if val and val not in found and any(ch.isdigit() for ch in val):
                    found.append(val)
        return found

    # --- 5.7 RIFF/WAVE Audio Analysis & Embedded Transcript -----------------

    def audio_transcribe(self, audio_b64: str, language: str = "auto") -> dict[str, Any]:
        """Inspect RIFF/WAVE PCM signal metrics and extract embedded TXT/LIST chunks."""
        data = _decode_b64(audio_b64)
        if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
            raise ValueError("audio_b64 must be a valid RIFF/WAVE payload")

        sample_rate = 0
        channels = 1
        bits_per_sample = 16
        pcm_data = b""
        embedded_texts: list[str] = []

        pos = 12
        while pos + 8 <= len(data):
            chunk_id = data[pos : pos + 4]
            chunk_size = struct.unpack("<I", data[pos + 4 : pos + 8])[0]
            chunk_start = pos + 8
            chunk_end = min(len(data), chunk_start + chunk_size)
            chunk_payload = data[chunk_start:chunk_end]
            pos = chunk_start + chunk_size + (chunk_size % 2)

            if chunk_id == b"fmt " and len(chunk_payload) >= 16:
                _fmt, channels, sample_rate, _byte_rate, _align, bits_per_sample = struct.unpack(
                    "<HHIIHH", chunk_payload[:16]
                )
            elif chunk_id == b"data":
                pcm_data = chunk_payload
            elif chunk_id in (b"TXT ", b"TEXT", b"NOTE", b"ICMT", b"INAM"):
                txt = chunk_payload.rstrip(b"\x00").decode("utf-8", errors="ignore").strip()
                if txt:
                    embedded_texts.append(txt)
            elif chunk_id == b"LIST" and len(chunk_payload) >= 4:
                sub_pos = 4 if chunk_payload[:4] in (b"INFO", b"adtl") else 0
                parsed_sub = False
                while sub_pos + 8 <= len(chunk_payload):
                    sub_size = struct.unpack("<I", chunk_payload[sub_pos + 4 : sub_pos + 8])[0]
                    sub_start = sub_pos + 8
                    sub_end = min(len(chunk_payload), sub_start + sub_size)
                    if sub_end < sub_start:
                        break
                    sub_txt = (
                        chunk_payload[sub_start:sub_end]
                        .rstrip(b"\x00")
                        .decode("utf-8", errors="ignore")
                        .strip()
                    )
                    if sub_txt:
                        embedded_texts.append(sub_txt)
                        parsed_sub = True
                    sub_pos = sub_start + sub_size + (sub_size % 2)
                if not parsed_sub:
                    fallback_txt = (
                        chunk_payload[sub_pos:]
                        .rstrip(b"\x00")
                        .decode("utf-8", errors="ignore")
                        .strip()
                    )
                    if fallback_txt:
                        embedded_texts.append(fallback_txt)

        if sample_rate <= 0:
            raise ValueError("WAV payload is missing a valid fmt chunk")

        bytes_per_sample = max(1, bits_per_sample // 8)
        frame_size = max(1, channels * bytes_per_sample)
        num_frames = len(pcm_data) // frame_size
        duration_seconds = round(num_frames / sample_rate, 4)

        samples: list[float] = []
        if pcm_data:
            if bits_per_sample == 8:
                samples = [(b - 128) / 128.0 for b in pcm_data]
            elif bits_per_sample == 16:
                count = len(pcm_data) // 2
                raw_shorts = struct.unpack(f"<{count}h", pcm_data[: count * 2])
                samples = [s / 32768.0 for s in raw_shorts]
            elif bits_per_sample == 32:
                count = len(pcm_data) // 4
                raw_ints = struct.unpack(f"<{count}i", pcm_data[: count * 4])
                samples = [s / 2147483648.0 for s in raw_ints]

        if samples:
            rms = round(math.sqrt(sum(s * s for s in samples) / len(samples)), 6)
            peak = round(max(abs(s) for s in samples), 6)
            if len(samples) > 1:
                crossings = sum(
                    1
                    for i in range(1, len(samples))
                    if (samples[i] >= 0.0) != (samples[i - 1] >= 0.0)
                )
                zcr = round(crossings / (len(samples) - 1), 6)
            else:
                zcr = 0.0
        else:
            rms = 0.0
            peak = 0.0
            zcr = 0.0

        is_silent = bool(rms < 1e-4 or peak < 1e-3)
        transcript = " ".join(embedded_texts).strip()
        detected_lang = (
            (detect_language(transcript) if transcript else "en")
            if language == "auto"
            else language
        )

        return {
            "format": "WAV",
            "duration_seconds": duration_seconds,
            "sample_rate": sample_rate,
            "channels": channels,
            "bits_per_sample": bits_per_sample,
            "rms": rms,
            "peak": peak,
            "zero_crossing_rate": zcr,
            "is_silent": is_silent,
            "text": transcript,
            "transcript": transcript,
            "language": detected_lang,
        }


# ---------------------------------------------------------------------------
# 6. ToolRegistry integration
# ---------------------------------------------------------------------------


def local_ai_tools(engine: LocalAIEngine | None = None) -> list[Tool]:
    """Expose :class:`LocalAIEngine` capabilities as MCP-compatible :class:`Tool`s."""
    eng = engine if engine is not None else LocalAIEngine()

    def _nlp_extract_entities(text: str, language: str = "auto") -> dict[str, Any]:
        return eng.extract_entities(text, language=language)

    def _semantic_search(
        query: str,
        documents: list[Any] | None = None,
        namespace: str = "default",
        top_k: int = 5,
    ) -> dict[str, Any]:
        return eng.semantic_search(
            query,
            documents=documents,
            namespace=namespace,
            top_k=top_k,
        )

    def _ml_analyze(task: str, **kwargs: Any) -> dict[str, Any]:
        return eng.ml_analyze(task, **kwargs)

    def _document_ocr(
        content_b64: str | None = None,
        text: str | None = None,
        extract_fields: bool = True,
    ) -> dict[str, Any]:
        return eng.document_ocr(
            content_b64=content_b64,
            text=text,
            extract_fields=extract_fields,
        )

    return [
        Tool(
            name="nlp_extract_entities",
            description=(
                "Extract structured entities (PERSON, LOCATION, ORG, DATE, MONEY, PERCENT, "
                "EMAIL, URL, PHONE) and keywords from Arabic, English, or mixed text."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "language": {"type": "string", "default": "auto"},
                },
                "required": ["text"],
            },
            risk=RiskLevel.READ_ONLY,
            fn=_nlp_extract_entities,
        ),
        Tool(
            name="semantic_search",
            description=(
                "Run hybrid Arabic/English semantic search over indexed or inline documents."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "documents": {"type": "array"},
                    "namespace": {"type": "string", "default": "default"},
                    "top_k": {"type": "integer", "default": 5},
                },
                "required": ["query"],
            },
            risk=RiskLevel.READ_ONLY,
            fn=_semantic_search,
        ),
        Tool(
            name="ml_analyze",
            description=(
                "Run local CPU machine-learning tasks: classify (k-NN), forecast "
                "(linear regression), cluster (k-means++), or anomaly (MAD Z-score)."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "enum": ["classify", "forecast", "cluster", "anomaly"],
                    },
                },
                "required": ["task"],
            },
            risk=RiskLevel.READ_ONLY,
            fn=_ml_analyze,
        ),
        Tool(
            name="document_ocr",
            description=(
                "Extract text and structured fields (dates, amounts, emails, urls, "
                "organizations, persons, locations, invoice_numbers) from PDF, PNG, or HTML/SVG."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "content_b64": {"type": "string"},
                    "text": {"type": "string"},
                    "extract_fields": {"type": "boolean", "default": True},
                },
            },
            risk=RiskLevel.READ_ONLY,
            fn=_document_ocr,
        ),
    ]


__all__ = [
    "LocalAIEngine",
    "detect_language",
    "local_ai_tools",
    "normalise_arabic",
]
