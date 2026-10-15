# مستودع مُضاف (Vendored) — ArtCraft Launcher

> **لماذا يعيش هذا الملف في `docs/` لا داخل `packages/craft-launcher/`؟**
> لأن الإذن باستخدام علامة ArtCraft التجارية (الفقرة 1 من
> [`docs/brand/LICENSE-brand.txt`](../packages/craft-launcher/docs/brand/LICENSE-brand.txt))
> يشترط أن تكون الشجرة **نسخة من المستودع الأصلي**، وأي ملف نضيفه نحن داخلها يجعلها
> حرفيًا «نسخة معدَّلة». لذلك تبقى الشجرة **مطابقة بايت‑ببايت** للمستودع الأصلي، وكل توثيقنا نحن
> — هذا الملف، الإشعارات، الـmanifest — يعيش خارجها. انظر
> [`docs/THIRD_PARTY_POLICY.md`](THIRD_PARTY_POLICY.md) القاعدة **T-3**.

هذه شجرة **نسخة مثبَّتة غير مُعدَّلة** من مشروع
[storytold/craft-launcher](https://github.com/storytold/craft-launcher) (ArtCraft Launcher)،
مستضافة هنا لدمجه مع منصة `cela_use_browser` وفق
[ADR-0015](../adr/0015-craft-launcher-integration.md) و
[خطة الدمج](CRAFT_LAUNCHER_PLAN.md).

| | |
|---|---|
| **الموقع في الريبو** | `packages/craft-launcher/` |
| **المصدر** | `https://github.com/storytold/craft-launcher.git` |
| **الالتزام المثبَّت** | `0f295d4b9c90d15c2e38e36c322bdbc0fa943a54` |
| **الإصدار** | `0.4.1` (Cargo workspace، Rust 2024، `rust-version = 1.90`) |
| **الرخصة** | MIT أو Apache-2.0 (اختر ما تشاء) — `LICENSE-MIT` و`LICENSE-APACHE` |
| **الحالة** | نسخة مقطوعة (snapshot) — لا يوجد remote هنا؛ التحديث يدوي بالإجراء أدناه |
| **بصمة النزاهة** | [`docs/vendored/craft-launcher.sha256`](vendored/craft-launcher.sha256) — مُتحقَّق منها آليًا |

## ما الذي يبقى كما هو

الشجرة بأكملها **نسخة طبق الأصل** من المصدر عند الالتزام المذكور — بما فيها
`NOTICE` و`ATTRIBUTION.md` و`AGENTS.md` و`CLAUDE.md` و`packaging/` و`assets/`
وملفات الرخصة. **لا شيء فيها محلي**: لا هذا الملف، ولا أي استثناء.

أي تعديل مستقبلي على الكود يُقترح **أعلى** في مستودع `storytold/craft-launcher` أولًا؛
الشجرة هنا تتلقى تحديثات مصقولة لا ابتكارات محلية، حتى لا تتشظى عن الأصل.

هذا ليس تفضيلًا تنظيميًا فحسب — إنه **الشرط القانوني** للاستضافة: الإذن بالعلامة
التجارية يسقط تلقائيًا («The permission ends automatically for anyone who breaks
these terms») إذا أصبحت الشجرة نسخة معدَّلة ولم تُزل الشعارات منها.

## الرخصة والعلامات التجارية (مُلزِم)

1. **الكود** مرخَّص بـ MIT أو Apache-2.0. يبقى كل من `LICENSE-MIT` و`LICENSE-APACHE`
   و`NOTICE` و`ATTRIBUTION.md` في جذر المجلد، وأي توزيع يُرفق معه إشعاراتها.
2. **علامة ArtCraft التجارية** (الشعارات في `docs/brand/` و`assets/app-icon/` واسم
   ArtCraft نفسه) **ليست مفتوحة المصدر** وتخضع لـ`docs/brand/LICENSE-brand.txt`.
   استضافتنا النسخة الأصلية كما هي مع إشعاراتها تقع تحت الإذن «as part of copies of
   this repository». أي توزيع مُعدَّل تحت اسم آخر يجب أن يزيل الشعارات وفق ترخيص العلامة.
3. المواد التابعة لجهات أخرى (خطوط، أيقونات) مرخَّصة برخصها المذكورة في
   `ATTRIBUTION.md` — لا تُحذف.
4. **لا تُوزَّع هذه الشجرة داخل حزمنا المنشورة**: مستبعدة صراحةً من الـ`sdist` في
   `pyproject.toml`، وغير مُدرجة في الـ`wheel` (الذي يحزم حزم `os_*` الأولى فقط).

## التحقق الآلي من النزاهة

كل ملف في الشجرة مُسجَّل ببصمة `sha256` في
[`docs/vendored/craft-launcher.sha256`](vendored/craft-launcher.sha256).
`tests/test_vendored_integrity.py` يتحقق منها **دون شبكة** في كل تشغيل اختبار،
و`launcher-ci.yml` يقارنها كذلك بالنسخة الأصلية عند الالتزام المثبَّت.

```bash
# تحقّق محلي (بلا شبكة)
python scripts/vendored_manifest.py verify

# أعد توليد الـmanifest بعد تحديث الشجرة عمدًا
python scripts/vendored_manifest.py write --commit <new-sha>
```

أي **إضافة أو حذف أو تعديل** داخل `packages/craft-launcher/` يُفشل الاختبار.
هذا مقصود: الانحراف الصامت عن الأصل هو بالضبط ما يُسقط إذن العلامة التجارية.

## حدود الاستخدام داخل المنصة

وفق قواعد المشروع الأصلية (`AGENTS.md`) التي نلتزم بها هنا أيضًا:

- التنزيلات تأتي من `https://github.com/storytold/<app>/releases` فقط (ومؤشر
  التنزيلات الخاص بـGitHub)، ولا يُثبَّت أي تنزيل دون التحقق من `SHA256SUMS.txt`.
- المحوّل `os_launcher` في هذه الريبو يقرأ **الكتالوج والإصدارات** فقط (قراءة)، ولا
  يُشغِّل مُثبِّتات ولا يتجاوز سياستي `SSRFGuard`/`EgressPolicy`.
- `os_launcher` **لا يقرأ الشجرة المُضافة وقت التشغيل إطلاقًا** — لا `open()` ولا
  `Path` إليها؛ العلاقة هي مرآة عقد (`catalog.py` ↔ `catalog.rs`) موثَّقة في
  docstrings فقط. هذا يُبقي الشجرة معزولة عن مسار التنفيذ.
- "Agents never land or approve a third-party app": أي صف كتالوج أو مصدر تنزيل
  لمستودع خارج `storytold/` قرار مُشرف بشري وحده.

## كيف تُحدَّث هذه النسخة

```bash
# 1) حدّث الالتزام المرجعي هنا وفي docs/CRAFT_LAUNCHER_PLAN.md
git clone https://github.com/storytold/craft-launcher.git /tmp/craft-launcher
cd /tmp/craft-launcher && git checkout <new-commit> && NEW=$(git rev-parse HEAD)

# 2) افحص diffs الرخصة والعلامة أولًا — تغيّرت الشروط = أعد تقييم الدمج كله
git diff <old-sha>..<new-sha> -- LICENSE-MIT LICENSE-APACHE NOTICE \
    ATTRIBUTION.md docs/brand/LICENSE-brand.txt

# 3) استبدل الشجرة (احذف ثم انسخ — نظيفة، بلا أي ملف محلي)
cd /path/to/open_source-
rm -rf packages/craft-launcher
mkdir -p packages/craft-launcher
(cd /tmp/craft-launcher && tar --exclude='.git' -cf - .) | tar -xf - -C packages/craft-launcher

# 4) أعد توليد بصمة النزاهة بالالتزام الجديد
python scripts/vendored_manifest.py write --commit "$NEW"

# 5) حدّث الالتزام في هذا الملف وفي docs/CRAFT_LAUNCHER_PLAN.md
#    وdocs/vendored/craft-launcher.sha256 (يُولَّد تلقائيًا في الخطوة 4)

# 6) زامِن مرآة العقد في packages/os_launcher/os_launcher/catalog.py مع catalog.rs

# 7) شغّل الفحوصات
make test && make launcher-test
```

**لا تعدّل الشجرة يدويًا أبدًا.** إن احتجت تغييرًا فيها، ارفعه للمستودع الأصلي أولًا ثم حدّث
الـsnapshot. الاستثناء الوحيد هو `make defork-launcher` — وهو مُصمَّم للـ**forks**
لا لهذا الريبو (انظر أدناه).

## لمن يعمل fork من هذا الريبو

رخصة العلامة التجارية تُلزم أي نسخة معدَّلة أو مشتقة **بإزالة شعارات ArtCraft**
(أو استبدالها). لتسهيل ذلك:

```bash
make defork-launcher        # يطلب علم تأكيد صريح قبل المساس بأي شيء
```

السكربت يزيل `docs/brand/` و`assets/app-icon/` و`assets/social/` ويعطّل البناء
المعتمد عليها. **لا تشغّله في هذا الريبو**: إزالة الشعارات من نسختنا تجعلها «نسخة
معدَّلة» وتكسر `packaging/icons.sh`، ونحن أصلًا لا نبني اللانشر كجزء من منتجنا.

## بناء المشروع المضاف

مستقل تمامًا عن سلسلة Python (لا يشارك `pyproject.toml` ولا CI الرئيسي):

```bash
cd packages/craft-launcher
cargo test --workspace --locked   # أو: cargo xtask ci
```

أو من جذر الريبو: `make launcher-test` (يتطلب مُصرِّف Rust 1.90+).
