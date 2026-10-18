# سياسة الأمان

هذا المستودع يعالج **كودًا غير موثوق ومدخلات شبكية غير موثوقة** بتصميمه، لذا الحدّ بين
«خطأ» و«ثغرة» مهم: أي شيء يسمح لتنفيذ معزول بالوصول إلى المضيف، أو بتجاوز `SSRFGuard`
أو `EgressPolicy`، أو بعبور بوابة موافقة HITL — هو **بلاغ أمني لا issue**.

هذا الملف يحدّد أين تُبلّغ وماذا يُتوقَّع، بالاتساق مع
[`CONTRIBUTING.md`](CONTRIBUTING.md#security-vulnerabilities).

---

## ١) الإصدارات المدعومة

| الإصدار | الحالة |
|---|---|
| `0.1.0` (alpha على `main`) | ✅ مدعوم — تُصلَح الثغرات على `main` أولًا |
| أي فرع/وسوم إصدار أخرى | لا يوجد بعد؛ لا توجد فروع صيانة تاريخية |

لا توجد حزمة منشورة على فهرس عام تحمل رقم إصدار مدعومًا (`pyproject` = `0.1.0`،
ونطاق النشر يستثني الشجرة المُضافة)؛ ما يهمّ هو **الالتزام على `main`** الذي تعمل عليه.

## ٢) كيف تُبلّغ (بلا issue عام)

1. **لا تفتح issue عامًا** بثغرة قابلة للاستغلال، ولا ترفق PoC في تعليق عام.
2. القناة المفضّلة: **الإبلاغ الخاص على GitHub** — `Security → Report a vulnerability`
   في هذا المستودع (يُنشئ مسودة advisory خاصة).
3. إن لم تكن الخاصية مُفعّلة على هذا المستودع (أو على fork): راسل مالك المستودع **خاصّةً**
   عبر العنوان الظاهر في ملفه الشخصي على GitHub — وهو ما ينصّ عليه `CONTRIBUTING.md`.

ضمّن في البلاغ ما يكفي لإعادة الإنتاج دون تسريب بيانات حسّاسة:

- السلوك المتوقَّع مقابل الفعلي، وأثره (`مضيف ↔ صندوق`، `شبكة`، `تجاوز موافقة`).
- أصغر خطوات إعادة إنتاج — أو اختبار pytest فاشل؛ هذه أسرع وسيلة للفهم هنا.
- الإصدار/الالتزام، ونظام التشغيل، و`python -V`، وقيمة `CELA_SANDBOXED` إن وُجدت.
- أي متغيّرات بيئة فعّالة (مثل `CELA_MCP_ALLOW_HOST_EXEC=1` أو `CELA_REACH_ENABLE=1`)
  **دون** لصق قيم الأسرار.

## ٣) داخل النطاق

- وصول تنفيذ داخل الصندوق إلى المضيف (هروب من `setrlimit`/المهلة/شبكة الاسم).
- تجاوز `SSRFGuard` أو `EgressPolicy` — **بما في ذلك قفزات إعادة التوجيه**.
- عبور بوابة HITL أو التلاعب بقرارها (موافقة تُنفَّذ دون مُقرِّر).
- تزوير أو تخمين الرموز: `CELA_API_TOKEN`، `CELA_PRIVATE_API_TOKEN`، تذاكر WebSocket،
  أسرار الويب هوك (ActivePieces / WaAPI).
- حقن أوامر عبر وصف أداة MCP أو صفحة مقروءة يؤدي إلى تنفيذ أداة دون موافقة.
- التلاعب بسجل النتائج (`.cela/ledger.jsonl`) أو بطاقات المهارات أو الذاكرة بحيث يتغيّر
  قرار لاحق، أو تسريب أسرار عبرها.
- تسريب أسرار في السجلات أو الـartifacts أو تقارير الأخطاء (توجد تنقية في
  `os_core/redact.py` — أي إفلات منها بلاغ).

## ٤) خارج النطاق

- **`packages/craft-launcher/`** — شجرة **طرف ثالث** مطابقة للأصل بايت‑ببايت
  ([`docs/VENDORED-craft-launcher.md`](docs/VENDORED-craft-launcher.md)). أبلِغ
  المنبع: <https://github.com/storytold/craft-launcher>. أي تعديل هنا يُسقط إذن العلامة.
- **`packages/legacy/`** — غير مُضمَّن في حزم النشر ولا في `ruff`.
- مخرجات ماسح آلي بلا إثبات أثر على هذا الكود (لا نُغلق تنبيهات لإرضاء أداة).
- ما يتطلّب مسبقًا وصولًا محليًا بامتيازات المستخدم نفسه — وتحديدًا **خوادم MCP على
  stdio** و**قنوات CLI في `os_reach`**: مغلقة افتراضيًا، وتحتاج إذنًا صريحًا، وتعمل
  بامتيازات عملية `cela`. هذا **مُصمَّم ومُعلَن** في README، لا خلل.
- جودة/هلوسة نموذج العرض التجريبي (`--demo`): نموذج استدلالي ثابت، ليس مكوّن أمان.

## ٥) حدود معروفة ومُعلَنة (لا تُبلّغ عنها كمجهول)

- **الصندوق المحلي ليس حدًّا آليًا للآلة الافتراضية.** `setrlimit` + مهلة +
  `unshare --net`، وإن رفض المضيف `unshare` تُبلّغ المنصة `unverified` بصدق بدل
  ادّعاء العزل. لتنفيذ كود خصم متعدد المستأجرين استخدم واجهة `CodeExecBackend`
  مع E2B/Firecracker.
- **لا Postgres/Redis حيّين في CI**؛ `DistributedRunStore` مُختبَر بمزوّدين مزيّفين.
- **لا تدقيق أمني خارجي، ولا برنامج مكافآت، ولا عملية CVE رسمية.**

## ٦) ما تتوقّعه منّا

- إشعار باستلام خلال **أيام قليلة** — جهد معقول، لا التزام تعاقدي (المشروع
  alpha بصيانة محدودة).
- عند التأكيد: إصلاح على `main`، ونشر advisory، ونسب الفضل إليك إن رغبت.
- إن لم نعتبره ثغرة أمنية نشرح السبب ونحوّله إلى issue عام برضاك.

## ٧) تحقّق بنفسك قبل الإبلاغ

```bash
make check     # ruff + فحص حداثة docs/EVIDENCE.md + المجموعة كاملة (بلا شبكة)
```

---

## English (condensed mirror)

**Reporting:** never open a public issue for an exploitable flaw. Use GitHub
**private vulnerability reporting** (`Security → Report a vulnerability`); if it is
disabled here, contact the repository owner privately via their GitHub profile
address, as `CONTRIBUTING.md` states. Include a minimal reproduction — a failing
pytest is ideal — plus commit, OS, `python -V`, and which env flags were on (never
paste secret values).

**In scope:** sandbox escape (host reach from `setrlimit`/timeout/netns); any bypass
of `SSRFGuard` or `EgressPolicy` **including redirect hops**; crossing a HITL approval
gate; forged/guessed `CELA_API_TOKEN`, `CELA_PRIVATE_API_TOKEN`, WebSocket tickets, or
webhook secrets; injection through an MCP tool description or a fetched page that
reaches a tool call without approval; tampering with the run ledger, skill cards, or
memory to change a later decision; secret leakage past `os_core/redact.py`.

**Out of scope:** `packages/craft-launcher/` (pristine third-party snapshot — report
upstream at <https://github.com/storytold/craft-launcher>); `packages/legacy/`;
scanner output with no demonstrated impact; anything predicated on already having
local user-level access — notably **stdio MCP servers** and **`os_reach` CLI channels**,
which are off by default, require explicit opt-in, and run with `cela`'s privileges by
design; and hallucinations of the fixed `--demo` model.

**Known and declared:** the local sandbox is not a VM boundary — when `unshare` is
unavailable the platform reports `unverified` instead of claiming isolation; no live
Postgres/Redis in CI (fakes only); no external audit, no bounty, no CVE process.

**Expectations:** acknowledgement within a few days (best effort, alpha project), a fix
on `main`, an advisory, and credit if you want it.
