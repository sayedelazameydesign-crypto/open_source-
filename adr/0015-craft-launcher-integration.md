# ADR-0015: ArtCraft Launcher — استضافة + محوّل، قراءة فقط، وسياسة قبل كل طلب

**التاريخ:** 2026-10-14 · **الحالة:** مقبول ومُنفَّذ للمراحل 0–4 (تبقّى 5–7 في خطة الدمج) ·
**عدّله:** [ADR-0018](0018-third-party-ip-governance.md) في 2026-10-15 — القرار نافذ كما هو،
لكن `VENDORED.md` المذكور أدناه نُقل إلى `docs/VENDORED-craft-launcher.md` وصار التطابق
مُثبَتًا ببصمة `sha256`. انظر «ما استقرّ بعد التنفيذ» في آخر هذا الملف.
**المصدر:** [`storytold/craft-launcher`](https://github.com/storytold/craft-launcher)
التزام `0f295d4b9c90d15c2e38e36c322bdbc0fa943a54` (v0.4.1) — MIT أو Apache-2.0
(شعارات ArtCraft علامة تجارية بترخيص خاص — `NOTICE` و`docs/brand/LICENSE-brand.txt`)

## السياق

طلب المالك إضافة **ArtCraft Launcher** إلى نظامه على هذه الريبو. بعد قراءة كود
المشروعين (لا وصفهما):

* craft-launcher = **تطبيق سطح مكتب Rust خالص** (egui/eframe + WASM عبر trunk)،
  يُثبِّت ويحدِّث ويشغِّل ثلاثة عشر تطبيقًا من Craft App Suite من
  `github.com/storytold/<app>/releases` مع تحقق `SHA256SUMS.txt`. **ليس مكتبة** ولا
  خدمة؛ شجرته `crates/{core,native,ui-egui}` + `apps/` + `xtask` + `packaging`.
* قواعده الذهبية (`AGENTS.md`): «كل شيء من الشبكة عدائي»، لا `unwrap`/`panic`،
  لا `unsafe`، التنزيلات من إصدارات التطبيق وحدها، و«الوكلاء لا يعتمدون أبدًا
  صف كتالوج لجهة خارج `storytold/`» — وهي قواعد نلتزم بها حرفياً في المحوّل.
* منصتنا بايثون تشغّل وكيلاء ومعزولًا؛ **لا تُثبِّت برامج سطح مكتب**، ونواتها
  `pydantic` + `httpx` فقط، ونختبر «بلا شبكة».
* سوابق: ADR-0013 اختار «محوّل لا دمج» لـAgent-Reach لأنه مُثبِّت CLI؛ هنا الوضع
  مختلف — المالك يريد **استضافة الكود** أيضًا، والمشروع مستقل البناء (Cargo
  workspace) لا يزاحم حزم الريبو.

## القرار

1. **مساران معًا، لا واحد:**
   * **استضافة:** نسخة snapshot **طبق الأصل** في `packages/craft-launcher/`
     مثبَّتة على التزام معلوم، مع `VENDORED.md` (المصدر، الرخصة، حدود العلامة
     التجارية، إجراء التحديث). لا submodule، ولا تعديلات محلية على الكود —
     التحديثات تُصقل أعلى أولًا.
   * **محوّل:** حزمة `packages/os_launcher/` تملك **العقد** و**السياسة** على غرار
     `os_reach`: مرآة `catalog.rs`، تحليل `release.rs`/`feed.rs` بنفس قواعد
     اللينينسي، و`CraftReleaseClient` بنقل `httpx` محقون (الفيد أولًا — GitHub
     بديلة، سقف صلاحية 24 ساعة).
2. **قراءة فقط، مبدأ لا تنفيذًا:** لا أداة ولا نقطة نهاية تحمّل أو تُثبِّت أو
   تُشغِّل. الكتالوج والإصدارات ومقارنة النسخ وحدها. التثبيت يبقى مسؤولية تطبيق
   سطح مكتب؛ أي استقبال لاحق له يحتاج ADR جديدًا وHITL صارمًا.
3. **السياسة قبل كل طلب:** allow-list أربعة مضيفين
   (`api.storyteller.ai`, `api.github.com`, `github.com`, `objects.githubusercontent.com`)
   فوق أرضية `SSRFGuard` لا تُرفع، تُضيّق بـ`CELA_LAUNCHER_ALLOW_HOSTS` وتُستثنى
   بـ`CELA_LAUNCHER_DENY_HOSTS`. وأي asset يجب أن يبدأ بـ
   `https://github.com/storytold/<app>/releases/download/` أو يُسقط في التحليل.
4. **التفعيل صريح:** الأسطح تتطلب `CELA_LAUNCHER_ENABLE=1` (وإلا 503 ذاكرًا المتغيّر)،
   وأدوات `ToolRegistry` لا تُسجَّل إلا بنداء `register_launcher_tools` — لا سلوك
   تلقائي لمجرد الاستيراد.
5. **العقد مأخوذ من مصدره:** كل قاعدة بيانات في `os_launcher.catalog`/`contract`
   تعكس سطرًا محددًا في `crates/core`، واختبارات البنية (13 تطبيقًا،
   `repo == storytold/<id>`، هوية `printcraft`) تكرّر اختبارات `catalog.rs` نفسها.
6. **الأسرار لا تسافر:** كل رسالة عاملية تمر بـ`scrub_credentials` (عقد ADR-0011)،
   وفشل المصدر = 502 صريح برسالة منقّاة، لا استثناءات تتسرب.
7. **بناء مستقل:** `cargo` لا يلمس `pyproject.toml` ولا CI البايثون؛
   `launcher-ci.yml` بفلتر مسارات + `make launcher-test`.

## البدائل المرفوضة

| البديل | لماذا رُفض |
|---|---|
| نسخ الكود داخل `os_tools` أو `os_server` | مزج Rust workspace بحزم بايثون؛ فوضى بناء ومسؤوليات |
| git submodule | احتكاك التهيئة (`--recurse-submodules`)، وصعوبة رؤية diffs التحديث في PR واحد |
| محوّل فقط دون استضافة (نمط ADR-0013 حرفيًا) | المالك يريد الكود في نظامه؛ والاستضافة تجعل مرآة العقد قابلة للتحقق بجوار مصدرها |
| تنفيذ التثبيت عبر المنصة | المنصة لا تُثبِّت برامج؛ وسطح الهجوم (تشغيل مُثبِّتات طرف ثالث) لا يُبرَّر لقراءة الإصدارات |

## ما نُفِّذ في هذه الجولة (مُختبَر)

* `packages/craft-launcher/` + `VENDORED.md` (التزام `0f295d4b`).
* `packages/os_launcher/os_launcher/`: `catalog.py`, `contract.py`, `security.py`,
  `client.py`, `tools.py`.
* `os_server/launcher_api.py` + ربط `app.py` (‏`/api/launcher/*` + `/cela/v1/launcher/*`).
* `packages/web/app/launcher/page.tsx` (لوحة قراءة فقط عبر الوكيل الحالي).
* `tests/test_launcher.py` (30) + `tests/test_launcher_api.py` (17) — 713 اختبارًا
  ناجحًا إجمالًا، `ruff` نظيف.
* `Makefile` (‏`launcher-test`/`launcher-build`) + `.github/workflows/launcher-ci.yml`
  + استثناء `packages/craft-launcher` من ruff.

التفاصيل والمخاطر وخارطة الطريق المتبقية:
[`docs/CRAFT_LAUNCHER_PLAN.md`](../docs/CRAFT_LAUNCHER_PLAN.md).

## العواقب

* كل تحديث لـ`packages/craft-launcher` يمر عبر إجراء
  [`docs/VENDORED-craft-launcher.md`](../docs/VENDORED-craft-launcher.md) (التزام + مزامنة
  `catalog.py` + `make test && make launcher-test`)، ويتطلب فحص diffs الرخصة أولًا.
* أي فرع مستقبلي (تثبيت، تشغيل، mirror فيدي داخلي) يبدأ بـADR مستقل لأن الافتراض
  «المنصة قارئة فقط لهذه البيانات» مثبَّت هنا.

---

## ما استقرّ بعد التنفيذ (تعديل 2026-10-15 — ADR-0018)

القرار أعلاه **نافذ كما هو**: الاستضافة + المحوّل، لا submodule ولا بديل.
ثلاثة تفاصيل تنفيذية تغيّرت لأنها كانت تُضعف الشرط الذي تقوم عليه الاستضافة نفسها:

1. **نُقل `VENDORED.md` من داخل الشجرة إلى
   [`docs/VENDORED-craft-launcher.md`](../docs/VENDORED-craft-launcher.md).**
   البند ١ في «القرار» يشترط نسخة «طبق الأصل» — وملف نضيفه نحن داخلها ينفي ذلك
   حرفيًا، فيُخرج الشجرة من «نسخة من المستودع» (المأذونة) إلى «نسخة معدَّلة»
   (التي يجب أن تُزيل الشعارات). الشجرة الآن **مطابقة بايت‑ببايت** للأصل.

2. **«طبق الأصل» صارت خاصية مُختبَرة لا ادّعاءً مكتوبًا.** بصمة `sha256` لكل ملف في
   [`docs/vendored/craft-launcher.sha256`](../docs/vendored/craft-launcher.sha256)،
   يتحقق منها `tests/test_vendored_integrity.py` بلا شبكة، وتُعاد مقارنتها بالنسخة
   الأصلية في CI. digest الشجرة = digest النسخة الأصلية عند `0f295d4b`:
   `sha256:8e008dc770754f3765d0c8e545d9a16dc1224aa9bc6faff21b205719e1470039`.

3. **الشجرة مستبعدة من الـ`sdist`.** كانت `pyproject.toml` تضبط الـ`wheel` فقط،
   وhatchling يضع كل ملف متتبَّع في الـ`sdist` افتراضيًا — أي أن نشر الحزمة كان
   سيوزّع شعارات ArtCraft (وهي **ليست** مفتوحة المصدر) تحت ميتاداتا MIT الخاصة بنا.
   هذا لا يغيّر القرار، لكنه يُنفِّذ حدوده.

القواعد العامة المستخلصة — وتُسري على أي استضافة قادمة لا على هذه وحدها — في
[ADR-0018](0018-third-party-ip-governance.md) و
[`docs/THIRD_PARTY_POLICY.md`](../docs/THIRD_PARTY_POLICY.md).
