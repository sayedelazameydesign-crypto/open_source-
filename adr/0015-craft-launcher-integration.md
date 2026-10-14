# ADR-0015: ArtCraft Launcher — استضافة + محوّل، قراءة فقط، وسياسة قبل كل طلب

**التاريخ:** 2026-10-14 · **الحالة:** مقبول ومُنفَّذ للمراحل 0–4 (تبقّى 5–7 في خطة الدمج)
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

* كل تحديث لـ`packages/craft-launcher` يمر عبر إجراء `VENDORED.md` (التزام + مزامنة
  `catalog.py` + `make test && make launcher-test`)، ويتطلب فحص diffs الرخصة أولًا.
* أي فرع مستقبلي (تثبيت، تشغيل، mirror فيدي داخلي) يبدأ بـADR مستقل لأن الافتراض
  «المنصة قارئة فقط لهذه البيانات» مثبَّت هنا.
