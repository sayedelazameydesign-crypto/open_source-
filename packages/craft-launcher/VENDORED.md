# مستودع مُضاف (Vendored) — ArtCraft Launcher

هذه شجرة **نسخة مثبَّتة غير مُعدَّلة** من مشروع [storytold/craft-launcher](https://github.com/storytold/craft-launcher)
(ArtCraft Launcher)، مُستضافة هنا لدمجه مع منصة `cela_use_browser` وفق
[ADR-0015](../../adr/0015-craft-launcher-integration.md) و
[خطة الدمج](../../docs/CRAFT_LAUNCHER_PLAN.md).

| | |
|---|---|
| **المصدر** | `https://github.com/storytold/craft-launcher.git` |
| **الالتزام المثبَّت** | `0f295d4b9c90d15c2e38e36c322bdbc0fa943a54` |
| **الإصدار** | `0.4.1` (Cargo workspace، Rust 2024، `rust-version = 1.90`) |
| **الرخصة** | MIT أو Apache-2.0 (اختر ما تشاء) — `LICENSE-MIT` و`LICENSE-APACHE` |
| **الحالة** | نسخة مقطوعة (snapshot) — لا يوجد remote هنا؛ التحديث يدوي بالإجراء أدناه |

## ما الذي يبقى كما هو

كل ما عدا هذا الملف **نسخة طبق الأصل** من الشجرة عند الالتزام المذكور (بما فيها
`NOTICE` و`ATTRIBUTION.md` و`AGENTS.md` و`CLAUDE.md` و`packaging/` و`assets/`).
أي تعديل مستقبلي على الكود يُقترح **أعلى** في مستودع `storytold/craft-launcher` أولًا؛
الشجرة هنا تتلقى تحديثات مصقولة لا ابتكارات محلية، حتى لا تتشظى عن الأصل.

## الرخصة والعلامات التجارية (مُلزِم)

1. **الكود** مرخَّص بـ MIT أو Apache-2.0. يبقى كل من `LICENSE-MIT` و`LICENSE-APACHE`
   و`NOTICE` و`ATTRIBUTION.md` في جذر المجلد، وأي توزيع يُرفق معه إشعاراتها.
2. **علامة ArtCraft التجارية** (الشعارات في `docs/brand/` و`assets/app-icon/` واسم
   ArtCraft نفسه) **ليست مفتوحة المصدر** وتخضع لـ`docs/brand/LICENSE-brand.txt`.
   النسخ المعدَّلة أو الـforks **يجب أن تزيلها**. استضافتنا النسخة الأصلية كما هي
   مع إشعاراتها؛ أي توزيع مُعدَّل تحت اسم آخر يجب أن يزيل الشعارات وفق ترخيص العلامة.
3. المواد التابعة لجهات أخرى (خطوط، أيقونات) مرخَّصة برخصها المذكورة في
   `ATTRIBUTION.md` — لا تُحذف.

## حدود الاستخدام داخل المنصة

وفق قواعد المشروع الأصلية (`AGENTS.md`) التي نلتزم بها هنا أيضًا:

- التنزيلات تأتي من `https://github.com/storytold/<app>/releases` فقط (ومؤشر
  التنزيلات الخاص بـGitHub)، ولا يُثبَّت أي تنزيل دون التحقق من `SHA256SUMS.txt`.
- المحوّل `os_launcher` في هذه الريبو يقرأ **الكتالوج والإصدارات** فقط (قراءة)، ولا
  يُشغِّل مُثبِّتات ولا يتجاوز سياستي `SSRFGuard`/`EgressPolicy`.
- "Agents never land or approve a third-party app": أي صف كتالوج أو مصدر تنزيل
  لمستودع خارج `storytold/` قرار مُشرف بشري وحده.

## كيف تُحدَّث هذه النسخة

```bash
# 1) حدّث الالتزام المرجعي هنا وفي docs/CRAFT_LAUNCHER_PLAN.md
git clone https://github.com/storytold/craft-launcher.git /tmp/craft-launcher
cd /tmp/craft-launcher && git checkout <new-commit> && NEW=$(git rev-parse HEAD)

# 2) استبدل الشجرة (احذف ثم انسخ، ثم أعد كتابة VENDORED.md بذكر الالتزام الجديد)
cd /path/to/open_source-
rm -rf packages/craft-launcher
mkdir -p packages/craft-launcher
(cd /tmp/craft-launcher && tar --exclude='.git' -cf - .) | tar -xf - -C packages/craft-launcher

# 3) شغّل فحوصات المحوّل والاختبارات، ثم حدّث changelog الدمج إن وُجد
make test && make launcher-test
```

عند التحديث افحص diffs في `LICENSE-*` و`NOTICE` و`ATTRIBUTION.md` و
`docs/brand/LICENSE-brand.txt` أولًا؛ تغيّرت الرخصة أو شروط العلامة = أعد تقييم
الدمج كله قبل الدمج.

## بناء المشروع المضاف

مستقل تمامًا عن سلسلة Python (لا يشارك `pyproject.toml` ولا CI الرئيسي):

```bash
cd packages/craft-launcher
cargo test --workspace --locked   # أو: cargo xtask ci
```

أو من جذر الريبو: `make launcher-test` (يتطلب مُصرِّف Rust 1.90+).
