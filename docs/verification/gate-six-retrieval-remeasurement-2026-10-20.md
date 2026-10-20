# تقرير Gate 6 — إعادة القياس على الرأس الحالي وإعلان الخلفية والفضاء

**التاريخ:** 2026-10-20 · **الحالة:** مُنفَّذ ومُتحقَّق منه بالأمر المباشر · **المرجع المعماري:** [ADR-0022](../../adr/0022-stored-vectors-and-hybrid-retrieval.md) · [ADR-0023](../../adr/0023-embedding-backends-behind-flags.md) · [تقرير Gate Zero المنقول عن PR #31](gate-zero-pr31-2026-10-20.md)

## ١. نطاق التحقق

أغلق هذا التقرير الفجوة المفتوحة في Gate Zero / Gate 4:

1. إخراج `backend` و`space` (`model@dim` أو `null`) صراحةً في كلٍّ من `KnowRetriever.retrieval_report()` وتقرير `os_evals.retrieval.measure()` (على المستوى الأعلى وداخل `engine`).
2. تثبيت المستندات الستة للمجموعة الذهبية في `tests/fixtures/golden_corpus/{audio,evals,memory,runbook,sandbox,security}.md` مطابقةً حرفيًا لـ`CORPUS_TEXTS` في `tests/test_know.py` (‏7 قطع إجمالًا، 21 سؤالًا في `tests/fixtures/golden.jsonl`).
3. تشغيل أمري القياس المطلوبين على هذا الرأس مباشرةً:
   * `python -m os_evals retrieval --arm all`
   * `cela know eval --arm all`
   تحت كلٍّ من الوضع الافتراضي `off` (`space=null`) والوضع المحلي بدون شبكة `local` (`space=feature-hash-v1@512`) بعد الترحيل عبر `cela know reindex --yes --json`.
4. تفعيل السقف المسمّى `MAX_TOKENS = 512` في `os_know.chunking` عبر `os_core.tokens.token_budget_chars` (قرار ADR-0022 رقم ٨) دون المساس بالأرقام المنقولة تاريخيًا عن PR #31.

---

## ٢. الأوامر الدقيقة المُشغَّلة لإعادة الإنتاج

```bash
rm -f /tmp/gate6_know.sqlite
for f in tests/fixtures/golden_corpus/*.md; do
  cela know index "$f" --corpus docs --store-url sqlite:////tmp/gate6_know.sqlite
done

# (١) الوضع الافتراضي: CELA_EMBED_BACKEND غير مُعدّ -> backend="off", space=null
CELA_KNOW_STORE_URL=sqlite:////tmp/gate6_know.sqlite python -m os_evals retrieval --arm all
CELA_KNOW_STORE_URL=sqlite:////tmp/gate6_know.sqlite cela know eval --arm all

# (٢) ترحيل الفهرس في المكان إلى local -> backend="local", space="feature-hash-v1@512"
CELA_EMBED_BACKEND=local CELA_KNOW_STORE_URL=sqlite:////tmp/gate6_know.sqlite cela know reindex --yes --json

# (٣) إعادة القياس بعد الترحيل
CELA_EMBED_BACKEND=local CELA_KNOW_STORE_URL=sqlite:////tmp/gate6_know.sqlite python -m os_evals retrieval --arm all
CELA_EMBED_BACKEND=local CELA_KNOW_STORE_URL=sqlite:////tmp/gate6_know.sqlite cela know eval --arm all
```

> **ملاحظة حول فرق `min_score` الافتراضي بين الأَمرين:** يبني `python -m os_evals retrieval` مسترجع `KnowRetriever` بالقيمة الافتراضية للصنف (`min_score = 0.02`)، بينما يمرّر `_know_layer` في `cela know eval` القيمة `min_score = 0.01`، لذلك يُنشر كلا المخرجين أدناه حرفيًا بلا دمج صامت.

---

## ٣. النتائج المقيسة على هذا الرأس (21 سؤالًا / 6 مستندات / 7 قطع)

### ٣-أ. الوضع الافتراضي `backend="off"`, `space=null`

| الأمر | الذراع | recall@5 | MRR | empty_rate | سياق/استعلام (حرف) | dense_only_hits | modes | رمز الخروج |
|---|---|---|---|---|---|---|---|---|
| `python -m os_evals retrieval --arm all` (`min_score=0.02`) | `sparse` | `1.0` | `0.9762` | `0.0` | `1040.0` | `0` | `{"sparse": 21}` | `0` |
| `python -m os_evals retrieval --arm all` (`min_score=0.02`) | `dense` | `0.0` | `0.0` | `1.0` | `0.0` | `0` | `{"no-hits": 21}` | *(مُستثنى عند `off`)* |
| `python -m os_evals retrieval --arm all` (`min_score=0.02`) | `hybrid` | `1.0` | `0.9762` | `0.0` | `1040.0` | `0` | `{"sparse": 21}` | `0` |
| `cela know eval --arm all` (`min_score=0.01`) | `sparse` | `1.0` | `0.9762` | `0.0` | `1171.9` | `0` | `{"sparse": 21}` | `0` |
| `cela know eval --arm all` (`min_score=0.01`) | `dense` | `0.0` | `0.0` | `1.0` | `0.0` | `0` | `{"no-hits": 21}` | *(مُستثنى عند `off`)* |
| `cela know eval --arm all` (`min_score=0.01`) | `hybrid` | `1.0` | `0.9762` | `0.0` | `1171.9` | `0` | `{"sparse": 21}` | `0` |

* **`delta` (`hybrid - sparse`) في كِلا الأمرين:** `recall_at_k = 0.0`, `mrr = 0.0`, `avg_context_chars = 0.0`, `dense_only_hits = 0` (**`Δ = 0`**).

### ٣-ب. بعد `cela know reindex --yes` إلى `backend="local"`, `space="feature-hash-v1@512"`

مخرج `cela know reindex --yes --json`:

* `space_before = null` → `space_after = "feature-hash-v1@512"`
* `chunks = 7`, `reindexed = 7`, `vectors_before = 0` → `vectors_after = 7`, `batches = 1` (`batch_size = 64`), `last_truncated = 0`, `exit_code = 0`.

مخرج القياس الثلاثي بعد الترحيل:

| الأمر | الذراع | recall@5 | MRR | empty_rate | سياق/استعلام (حرف) | dense_only_hits | modes | رمز الخروج |
|---|---|---|---|---|---|---|---|---|
| `python -m os_evals retrieval --arm all` (`min_score=0.02`) | `sparse` | `1.0` | `0.9762` | `0.0` | `1040.0` | `0` | `{"sparse": 21}` | `1` *(بسبب سقوط `dense` المنفرد)* |
| `python -m os_evals retrieval --arm all` (`min_score=0.02`) | `dense` | **`0.4286`** | **`0.4286`** | **`0.5714`** | `94.3` | `9` | `{"dense": 9, "no-hits": 12}` | `failures: ["dense: recall@5=0.4286 < floor 0.8", "dense: mrr=0.4286 < floor 0.6"]` |
| `python -m os_evals retrieval --arm all` (`min_score=0.02`) | `hybrid` | `1.0` | `0.9762` | `0.0` | `1040.0` | `0` | `{"hybrid": 9, "sparse": 12}` | — |
| `cela know eval --arm all` (`min_score=0.01`) | `sparse` | `1.0` | `0.9762` | `0.0` | `1171.9` | `0` | `{"sparse": 21}` | `1` *(بسبب سقوط `dense` المنفرد)* |
| `cela know eval --arm all` (`min_score=0.01`) | `dense` | **`0.4286`** | **`0.4286`** | **`0.5714`** | `94.3` | `9` | `{"dense": 9, "no-hits": 12}` | `failures: ["dense: recall@5=0.4286 < floor 0.8", "dense: mrr=0.4286 < floor 0.6"]` |
| `cela know eval --arm all` (`min_score=0.01`) | `hybrid` | `1.0` | `0.9762` | `0.0` | `1171.9` | `0` | `{"hybrid": 9, "sparse": 12}` | — |

### ٣-ج. الاستنتاج الهندسي الصريح (بلا تجميل)

1. **بين `hybrid` و`sparse` على `local` (`feature-hash-v1@512`): `Δ = 0` تمامًا** (`recall_at_k = 0.0`, `mrr = 0.0`, `avg_context_chars = 0.0`, `dense_only_hits = 0`)، مع دخول المتجه فعليًا في دمج RRF في 9 من أصل 21 استعلامًا (`modes = {"hybrid": 9, "sparse": 12}`). **لا يوجد أي تفوّق جودة، ولا يُدَّعى أي تحسّن.**
2. **على الذراع `dense` منفردًا:** تسقط خلفية التجزئة السطحية `local` دون السقف المنشور (`0.4286 < 0.8` في `recall@5` و`0.4286 < 0.6` في `MRR`، وخروج `1`) لأن تشابه جيب التمام لحقيبة مقاطع حرفية/معجمية موحّدة الطول (`L2`) بين سؤال قصير وفقرة متعددة الجمل ينخفض تحت `MIN_DENSE_SCORE = 0.35` في 12 من 21 سؤالًا، ويشترك في المعجم نفسه مع `sparse` في الأسئلة التسعة المتبقية. هذا يثبت تجريبيًا أن `local` خلفية فحص أسلاك offline لا مُرمِّزًا دلاليًا، وأن إبقاء `off` افتراضيًا هو القرار الصحيح.
3. **ما لم يُقَس على هذا الرأس:** الخلفيتان `model` (`SentenceTransformerEmbedding`) و`provider` (`ProviderEmbedding`) تتطلبان أوزان نموذج محمّلة أو نقطة نهاية خارجية بمفتاح وإذن خروج؛ أداؤهما الدلالي على هذه المجموعة **غير مقيس offline على هذا الرأس** ولا يُستنتج من `local`.

---

## ٤. سقف التوكنز المسمّى `MAX_TOKENS = 512` والتمييز عن الأرقام المنقولة

* أرقام المتْن الطويل في PR #31 (عربي `262,915` حرفًا: `168 → 332` قطعة، سياق `23,158 → 4,071` حرفًا عند `recall@5 = 0.625, MRR = 0.3241`؛ إنجليزي `244,756` حرفًا بلا تغيير) تبقى موسومة صراحةً **«منقول عن PR #31 (2026-10-16)»** في [`docs/verification/gate-zero-pr31-2026-10-20.md`](gate-zero-pr31-2026-10-20.md) لأن المتْن الخارجي ذا الـ`262,915` حرفًا لم يكن ملفًا محفوظًا في الشجرة.
* على هذا الرأس جرى التحقق المباشر عبر `tests/test_retrieval_eval.py::test_chunking_enforces_max_tokens_ceiling_on_arabic_without_shifting_english` من السلوك الآلي لـ`MAX_TOKENS = 512`:
  * متْن عربي متعدد الفقرات (`5,044` حرفًا): `max_tokens=None` ينتج قطعة بحد أقصى `1,594` حرفًا = **`613 > 512`** توكن مقدَّر (`estimate_model_tokens`)؛ بينما الافتراضي `MAX_TOKENS = 512` يقيّد النافذة بـ`token_budget_chars(..., 512) = 1331` حرفًا فينتج أقصى قطعة `1,258` حرفًا = **`484 ≤ 512`** توكن مقدَّر.
  * متْن إنجليزي مناظر (`5,348` حرفًا): `token_budget_chars(..., 512) = 2048 > MAX_CHARS (1600)`، فيبقى التقطيع متطابقًا بايتًا ببايت (`1,590` حرفًا).
