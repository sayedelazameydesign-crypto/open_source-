# خطة إدماج ArtCraft Launcher في منصة `cela_use_browser` (`open_source-`)

**التاريخ:** 2026-10-14 · **الحالة:** القرارات مُعتمدة في [ADR-0015](../adr/0015-craft-launcher-integration.md)؛
المراحل 0–4 **مُنفَّذة ومُختبَرة** في هذه الجولة، والمراحل 5–7 المتبقية موصوفة في القسم 8.
**المصدر:** [`storytold/craft-launcher`](https://github.com/storytold/craft-launcher) —
التزام `0f295d4b9c90d15c2e38e36c322bdbc0fa943a54` (v0.4.1)، رخصة **MIT أو Apache-2.0**
مع قيود علامة تجارية على شعارات ArtCraft (انظر القسم 6).

هذه الخطة مبنية على **قراءة كود المشروعين** لا على الوصف التسويقي: استُنسخ المستودع
وفُحصت شجرته (`crates/core`, `crates/native`, `AGENTS.md`, `NOTICE`) وطُوِّق كل عقد
مُستعار باختبارات على هذه الشجرة قبل الاعتماد.

---

## 0) الملخص التنفيذي

ArtCraft Launcher تطبيق **Rust خالص** (egui/eframe + نسخة WASM) يُثبِّت ويحدِّث ويشغِّل
ثلاثة عشر تطبيقًا من Craft App Suite، ويقرأ إصداراتها من GitHub (وفيد `api.storyteller.ai`
أولًا). ليس مكتبة تُستورَد ولا خدمة تُنشر — هو **منتج مستقل**. والمنصة من جهتها منصة
وكيلاء (Python) لا تُثبِّت برامج سطح مكتب أبدًا.

لذلك الدمج المُعتمد **مساران معًا**، لا مسار واحد:

> **استضافة (Hosting) + محوّل (Adapter):**
> 1. شجرة Rust تُستضاف **طبق الأصل** في `packages/craft-launcher/` (التزام مثبَّت +
>    `docs/VENDORED-craft-launcher.md`)، تُبنى وتُختبر بمسار CI خاص بها مستقل عن Python.
> 2. حزمة `packages/os_launcher/` — محوّل **قراءة فقط** على غرار `os_reach` (ADR-0013):
>    يملك العقد والسياسة، يقرأ الكتالوج والإصدارات عبر `EgressPolicy` + `SSRFGuard`،
>    ويكشف قدرات كأدوات `ToolRegistry` و/أو أسطح HTTP خلف `CELA_LAUNCHER_ENABLE=1`.
>    **لا يُنزِّل ولا يُثبِّت ولا يُشغِّل** أي شيء — لأن المنصة لا تفعل ذلك عبر نداء نموذج.

## 1) القرارات المعتمدة

| السؤال | القرار | لماذا |
|---|---|---|
| موقع الكود المضاف | `packages/craft-launcher/` نسخة snapshot مثبَّتة (لا submodule) | المونوريبو يعتمد `packages/`، والـsnapshot يبقى قابلًا للبناء دون `.gitmodules`، مع `docs/VENDORED-craft-launcher.md` يوثّق الالتزام وطريقة التحديث |
| أين يعيش العقد | `packages/os_launcher/` حزمة مستقلة (فصل مسؤوليات كـ`os_reach`) | عزل تبعيات؛ `os_tools` لا تعرف شيئًا عن ArtCraft؛ إمكانية استبدال أي طبقة |
| ماذا يقرأ المحوّل | الكتالوج (مرآة `catalog.rs`) + إصدارات GitHub + الفيد | هذا كل ما تلمسه المنصة فعليًا من اللانشر؛ التثبيت مسؤولية تطبيق سطح مكتب لا منصة خادم |
| التفعيل | `CELA_LAUNCHER_ENABLE=1` (opt-in)، والتسجيل في `ToolRegistry` يدوي فقط | نفس فلسفة `os_reach`: أداة شبكة لا تظهر لمجرد استيراد الحزمة |
| حدود الشبكة | allow-list أربعة مضيفين فقط (`api.storyteller.ai`, `api.github.com`, `github.com`, `objects.githubusercontent.com`) + أرضية SSRF لا تُرفع | اللانشر نفسه لا يتحدث إلا معهم؛ أي توسيع = قرار واعٍ |
| قاعدة التنزيلات | كل asset يجب أن يكون تحت `github.com/storytold/<app>/releases/download/` وإلا أُسقط | قاعدة `AGENTS.md` للانشر نفسه («كل شيء من الشبكة عدائي») |
| CI | `launcher-ci.yml` مسار منفصل بفلتر مسارات + `make launcher-test` | صدامات Rust لا تُبطئ CI بايثون؛ والاختبارات تتطلب حزم نظام egui |
| الرخصة | نُبقي `LICENSE-MIT`/`LICENSE-APACHE`/`NOTICE`/`ATTRIBUTION.md` كما هي، والشعارات بترخيص العلامة الخاص بها | التزام قانوني عند الاستضافة (القسم 6) |

## 2) ما نُفِّذ فعلًا في هذه الجولة

### المرحلة 0 — الاستضافة (Hosting)
* `packages/craft-launcher/` — نسخة طبق الأصل من التزام `0f295d4b` (‏4.6MB، 198 ملفًا)،
  تشمل `crates/` و`apps/` و`xtask/` و`packaging/` و`assets/` وكل ملفات الرخصة والإشعارات.
* `docs/VENDORED-craft-launcher.md` — المصدر، الالتزام، الرخصة، حدود العلامة التجارية،
  «ماذا يبقى كما هو»، وإجراء التحديث الكامل.
* استثناء المجلد من `ruff` في `pyproject.toml` (مثل `legacy`/`web`): كود مضاف لا نلمسه.

### المرحلة 1 — حزمة المحوّل `os_launcher`
* `catalog.py` — مرآة Python لـ`catalog.rs`: ثلاثة عشر تطبيقًا بألوانها وسماتها،
  هوية `pdfcraft`/`printcraft` القديمة، `bundle_id`/`website`/`stems` كدوال Rust نفسها.
* `contract.py` — مرآة `release.rs` + `feed.rs`: `AppRelease`/`ReleaseAsset`/`FeedSnapshot`،
  `parse_github_release` و`parse_sha256sums` و`parse_feed` **بنفس قواعد اللينينسي**:
  كل حقل اختياري، نوع خاطئ = غائب، سجل فاسد لا يفسد البقية، وروابط التنزيلات خارج
  إصدارات التطبيق تُسقط.
* `security.py` — `default_launcher_policy()` (أربعة مضيفين + deny/allow env)،
  `is_own_release_download()`، و`scrub_credentials()` بعقد `os_reach.security` نفسه.
* `client.py` — `CraftReleaseClient` غير متزامن بحقن `httpx` transport:
  الفيد أولًا (طلب واحد لكل التطبيقات)، GitHub كخطة بديلة، سقف صلاحية الفيد 24 ساعة،
  و`SHA256SUMS.txt` يملأ checksums الناقصة (digest الخاص بـGitHub يبقى مقدمًا).
* `tools.py` — ثلاث أدوات مُسجَّلة يدويًا فقط:
  `craft_list_apps` (قراءة محلية)، `craft_latest_release` و`craft_check_updates`
  (‏`risk=NETWORK`، رسائل أخطاء منقّاة الأسرار).

### المرحلة 2 — أسطح HTTP
* `os_server/launcher_api.py` — `GET /api/launcher/catalog`،
  `GET /api/launcher/apps/{id}/release`، `POST /api/launcher/check` +
  النسخة الخاصة `/cela/v1/launcher/*`، خلف توكن الأصل كما في `reach_api` (لا توكن ثانٍ).
* ربط في `app.py` مع `launcher_enabled`/`launcher_service`/`launcher_policy`؛ ومعطّل
  افتراضيًا يرد **503** ذاكرًا `CELA_LAUNCHER_ENABLE`، وفشل المصدر **502** برسالة منقّاة.

### المرحلة 3 — لوحة الويب
* `packages/web/app/launcher/page.tsx` — لوحة قراءة فقط (الكتالوج + فحص التحديثات) عبر
  وكيل `app/api/backend/*` الحالي؛ الرموز لا تصل للمتصفح. أي سطح backend معطّل يظهر
  كرسالة 503 صريحة لا يُخباها.

### المرحلة 4 — الاختبارات والتكامل
* `tests/test_launcher.py` (‏30 اختبارًا) — سلامة الكتالوج (13 تطبيقًا، `storytold/<id>`،
  printcraft)، صرامة أسماء الملفات و`SHA256SUMS`، هشاشة digest المزوَّر، قاعدة مضيف
  التنزيلات، أرضية SSRF فوق `ALLOW_HOSTS`، deny يسبق allow، الفيد القديم/العابث،
  انتقال الفيد→GitHub، وأدوات `ToolRegistry`.
* `tests/test_launcher_api.py` (‏17 اختبارًا) — المصادقة على النسختين، تعطيل افتراضي
  **يُثبت أنه لم يُطلِب أي شيء** (عدّاد طلبات)، الكتالوج، الإصدار، الفحص، 404/422/502.
* `pyproject.toml` — حزمة wheel + `pythonpath` + coverage، و`Makefile` (‏`launcher-test`،
  `launcher-build`)، و`.github/workflows/launcher-ci.yml` (cargo test/clippy للـcore
  + مساحة العمل كاملة continue-on-error).
* **الحصيلة:** 713 اختبارًا ناجحًا (666 سابقًا + 47 جديدًا)، `ruff` نظيف.

## 3) مخطط الطبقات

```
packages/os_launcher  (جديد — العقد والسياسة، بايثون)
   │  catalog.py   مرآة catalog.rs (بيانات ساكنة)
   │  contract.py  تحليل release.rs + feed.rs (عدائي → نماذج)
   │  security.py  EgressPolicy(4 hosts) + SSRFGuard + scrub + قاعدة storytold/
   │  client.py    feed-first → GitHub fallback (httpx، transport محقون)
   │  tools.py     ToolRegistry (قراءة فقط، تسجيل يدوي)
   │
   ├── os_tools.ToolRegistry  ←  craft_list_apps / craft_latest_release / craft_check_updates
   ├── os_server/launcher_api ←  /api/launcher/* + /cela/v1/launcher/* (opt-in)
   └── packages/web/app/launcher ← لوحة قراءة فقط عبر الوكيل الحالي

packages/craft-launcher  (مضيف — Rust workspace طبق الأصل)
   crates/core  ← مصدر العقد الذي تعكسه os_launcher
   crates/native, ui-egui, apps/…  ← تُبنى بـcargo لكن لا تتصل بها المنصة
```

## 4) عقد البيانات المُستعار (المصدر = `crates/core`)

| المفهوم | Rust | Python (`os_launcher`) |
|---|---|---|
| التطبيق | `CraftApp` + `SUITE[13]` | `CraftApp` + `SUITE` (نفس الترتيب والحقول) |
| الإصدار | `Release` / `Asset` | `AppRelease` / `ReleaseAsset` |
| Checksums | `parse_sha256sums`, `apply_sums`, `digest` بادئة `sha256:` | نفس الدوال والقواعد (64 hex، اسم ملف عارٍ) |
| الفيد | `Feed` (لينينسي، `MAX_AGE=24h`) | `FeedSnapshot.stale(max_age=86400)` |
| قاعدة التنزيل | `download_prefix(repo)` | `is_own_release_download(url, repo)` |
| التسمية | `version_label()` (semver أو tag) | `parse_tag_version()` ثم tag |

## 5) الأمان — القواعد الأربع المطبَّقة واختبارها

1. **أرضية SSRF لا تُرفع** — حتى لو ضبط مشغّل `CELA_LAUNCHER_ALLOW_HOSTS` على
   `169.254.169.254`، يبقى مرفوضًا (اختبار صريح).
2. **Allow-list أربعة مضيفين** افتراضيًا؛ `CELA_LAUNCHER_DENY_HOSTS` يسبق السماح.
3. **assets من إصدارات التطبيق وحدها** — رابط feed أو GitHub خارج
   `github.com/storytold/<app>/releases/download/` يُسقط في التحليل (لا يُرفض لاحقًا فقط).
4. **قراءة فقط** — لا أداة ولا نقطة نهاية تحمّل أو تُثبِّت أو تُشغِّل. وسم `NETWORK`
   على أدوات الشبكة يضعها أمام سياسة HITL كبقية أدوات الشبكة.

وكل رسالة خطأ عاملية تمر بـ`scrub_credentials` (بيانات اعتماد URL واستعلامات token).

## 6) الرخصة والعلامات التجارية (مُلزِم)

* الكود: **MIT أو Apache-2.0**. نحتفظ بالملفات الأربعة (`LICENSE-MIT`,
  `LICENSE-APACHE`, `NOTICE`, `ATTRIBUTION.md`) داخل `packages/craft-launcher/`؛
  أي توزيع يُرفق معه إشعاراتها.
* **اسم ArtCraft وشعاراته** (`docs/brand/`, `assets/app-icon/`) **علامات تجارية ليست
  مفتوحة المصدر** وتخضع لـ`docs/brand/LICENSE-brand.txt`: النسخ المعدَّلة والـforks
  **يجب أن تزيلها**. نحن نستضيف النسخة الأصلية كما هي؛ أي توزيع مُعدَّل لاحق تحت اسم
  آخر يزيل الشعارات أولًا.
* خطوط ومواد تابعة لجهات أخرى برخصها في `ATTRIBUTION.md` — لا تُحذف.

## 7) تشغيل وصيانة

```bash
# المنصة (بايثون) — أدوات اللانشر اختيارية
export CELA_LAUNCHER_ENABLE=1        # إلزامي لتفعيل الأسطح
make test                            # 713 اختبارًا

# مساحة Rust المضافة (مستقلة)
make launcher-test                   # cargo test --workspace --locked
```

**تحديث النسخة المضافة:** الإجراء الكامل في
[`docs/VENDORED-craft-launcher.md`](VENDORED-craft-launcher.md) —
استبدال snapshot + تحديث الالتزام هنا وفي ذلك الملف + إعادة توليد بصمة النزاهة
(`make vendored-write COMMIT=<sha>`) + مزامنة `catalog.py`
مع `catalog.rs` إن تغيّر الكتالوج + `make test && make launcher-test`.
فحص diffs في ملفات الرخصة أولًا؛ تغيّرت الشروط = إعادة تقييم الدمج كله.

> **لماذا لم يعد هذا الملف داخل الشجرة؟** كان `VENDORED.md` موضوعًا في
> `packages/craft-launcher/`، وهو ملف أضفناه نحن — أي أن الشجرة لم تكن نسخة
> مطابقة للأصل حرفيًا. وإذن العلامة التجارية يغطي «نسخ من هذا المستودع» فقط.
> نُقل التوثيق كله إلى `docs/` وصار التطابق **مُثبَتًا آليًا** ببصمة `sha256`
> لكل ملف. التفاصيل: [`THIRD_PARTY_POLICY.md`](THIRD_PARTY_POLICY.md) القاعدة T-3.

## 8) ما تبقّى (خارطة الطريق)

| المرحلة | المحتوى | ملاحظات |
|---|---|---|
| **5 — تنفيذ التثبيت (اختياري ومنفصل)** | إذا احتجنا يومًا تثبيتًا فعليًا، فهو **عبر التطبيق نفسه** (`artcraft-launcher` CLI/وضع headless إن أضافه upstream) لا عبر نموذج؛ يحتاج ADR جديد + HITL صارم + مراجعة بشرية لكل تنزيل | مقصود التأجيل: التثبيت مسؤولية سطح مكتب |
| **6 — فهرسة الملاحظات** | `notes` Markdown للإصدارات في فهرس بحث المنصة الدلالي (`LocalAIEngine`) | منخفض المخاطر، بيانات نصية فقط |
| **7 — توسيع الفيد** | خادم `api.storyteller.ai` للفيد إن احتجنا mirror داخليًا (`Feed::with_mirror` في upstream) | يعمل الآن بلاه؛ الأولوية منخفضة |

## 9) المخاطر والأسئلة المفتوحة

| الخطر | التخفيف |
|---|---|
| تشتت النسخة عن upstream (snapshot جامد) | `docs/VENDORED-craft-launcher.md` بإجراء تحديث مكتوب، وقاعدة «لا ابتكارات محلية»، وقاعدة مزامنة `catalog.py`↔`catalog.rs` مُختبَرة |
| تغيّر GitHub API أو الفيد | التحليل لينينسي ومُختبَر على مدخلات عدائية؛ الفادئ المقيس هو 502 الصريح لا الانهيار |
| استهلاك حد GitHub (60 طلب/ساعة بلا توكن) | الفيد أولًا (طلب واحد يغطي 13 تطبيقًا)، وأدوات `craft_latest_release` مفردة الطلب؛ يبقى سقف عملي للوكيل — يمكن لاحقًا ربط توكن عبر البيئة بحسب عقد ADR-0011 |
| إساءة قراءة الكتالوج على أنها «تثبيت» | الوصف صريح «Read-only: never downloads or installs» في كل أداة ونقطة نهاية ووثيقة |
| رخصة العلامة التجارية | القسم 6 + تذكير في `docs/VENDORED-craft-launcher.md`؛ أي fork مُوزَّع يزيل الشعارات |

**السؤال المفتوح الوحيد:** هل نحتاج يومًا «تشغيل» تطبيقات Craft من المنصة؟
الافتراض الحالي: **لا** — المنصة تشغّل وكيلاء ومهام، وتطبيقات Craft سطح مكتب للمستخدم
البشري. أي تغيير على هذا الافتراض يبدأ بـADR جديد.
