# لوحة المنصة — App Launchpad · Devbox · Database · Image Registry

تاريخ التنفيذ: 2026-10-20. الفرع: `arena/6892b6dc-open-source`.

## STATUS

**مُنفَّذة محليًا بحدود معلنة.** أربع صفحات قراءة فقط في `packages/web` تقرأ من نقطة
خادم واحدة جديدة `GET /platform/info` ومن `GET /ledger` القائمة. لا تنشر، ولا تسحب
صورة، ولا تنشئ قاعدة بيانات، ولا تشغّل كودًا من المتصفح. هذه ليست مراجعة مستقلة من
المستخدم، ولا شهادة جاهزية إنتاجية.

السبب في بناء الشاشات بهذا الشكل: أقسام منصة الاستضافة (ClawCloud Run) بأسمائها —
App Launchpad وDevbox وDatabase وImage Registry — هي **لوحة تحكم المنصة**، وليست
واجهات يملكها هذا المستودع. ما يملكه المستودع هو الحقيقة عن العملية التي يعرّفها:
صورتها، وضع تنفيذها، مخزنها، وسجل نتائجها. لذلك تعرض الصفحات هذه الحقائق وتُسمّي
الغياب صراحة بدل استنساخ أزرار لا تعمل.

## الصفحات

| الصفحة | المسار | المصدر الفعلي | ما تفعله | ما لا تفعله |
|---|---|---|---|---|
| **App Launchpad** | `/launchpad` | `/platform/info` + `/ledger?limit=5` | هوية التطبيق، الإصدار، مدة التشغيل، وضع demo/live، المنفّذ، المخزن، آخر النتائج | لا نشر/إعادة تشغيل/توسيع |
| **Devbox** | `/devbox` | `/platform/info` | وضع `CELA_CODE_EXEC_MODE`، الخلفية المختارة، حدّ العزل، عزل الشبكة، الأدوات المسجّلة، وقياس زمن استجابة حقيقي | لا محرّر كود ولا تنفيذ من المتصفح |
| **Database** | `/database` | `/platform/info` + `/ledger?limit=20` | نوع `RunStore` ودوامه وهدفه (بعد الحجب)، مسار السجل ومخططه، ملخّص النتائج وجدول التشغيلات | لا SQL ولا ترحيل ولا حذف |
| **Image Registry** | `/registry` | `/platform/info` | مرجع OCI، المضيف/المستودع/الوسم/البصمة، بصمة البناء ووقته، وأمر سحب للنسخ | لا سحب/دفع/حذف/إعادة وسم |

التنقل: مجموعة **المنصة** في الشريط الجانبي وفي قائمة الهاتف، مع `aria-current="page"`
لكل مسار، والعناوين في شريط التتبّع مأخوذة من نفس الخريطة.

## عقد `GET /platform/info`

يتطلب `Authorization: Bearer $CELA_API_TOKEN` كبقية سطح المنصة. GET فقط، بلا كتابة.
الشكل (مختصر):

```json
{
  "status": "ok",
  "product": "Celia.sh — Mission OS",
  "version": "0.1.0",
  "generated_at": "2026-10-20T08:00:00Z",
  "process": { "started_at": "...", "uptime_seconds": 3600.5, "python_version": "3.11.2",
               "python_implementation": "cpython", "platform": "linux", "machine": "x86_64" },
  "deployment": { "mode": "demo", "app_name": "celia-mission-os", "listen_port": 8080,
                  "region": null, "public_url_configured": false },
  "execution": { "kind": "scripted", "model_name": null, "goal_verification": "not_verified" },
  "sandbox": { "code_exec_mode": "disabled", "requested_backend": null,
               "backend": "DisabledBackend", "configuration_error": null,
               "executes_untrusted_code": false, "isolation": "none",
               "network_isolation": "n/a" },
  "store": { "backend": "InMemoryRunStore", "scope": "process", "durable": false,
             "target": null, "ledger": { "enabled": true, "path": ".cela/ledger.jsonl",
             "schema_version": 1, "entries": 0, "corrupt_lines": 0 } },
  "registry": { "configured": false, "source": null, "ignored_invalid_reference": false,
                "reference": null, "host": null, "repository": null, "tag": null,
                "digest": null, "build_sha": null, "build_time": null },
  "tools": { "count": 3, "registered": ["generate_deployment_plan_report", "..."] }
}
```

### ضمانات المحتوى

- **لا أسرار**: لا يُقرأ أي متغيّر بيئة ويُعاد إلا عبر `_safe_text` (طول محدود، بلا
  مسافات/محارف تحكم، ويرفض `@`) أو عبر تحليل مرجع OCI. `CELA_API_TOKEN` و
  `CELA_PRIVATE_API_TOKEN` لا يظهران في أي حقل.
- **لا اعتماد**: هدف Postgres يمرّ عبر `os_core.redact.scrub_credentials` قبل العرض،
  فيظهر `REDACTED` بدل كلمة المرور أو سرّ الاستعلام.
- **لا تخمين**: مرجع صورة غير صالح أو يحمل بيانات اعتماد يُرفض ويُبلَّغ عنه بـ
  `ignored_invalid_reference: true`، ولا يُعاد نصه. مرجع غائب يبقى `null` وتُعرض
  «غير مضبوط» بدل استنتاج وسم.
- **عزل الشبكة لا يُدّعى**: الوضع المحلي يبلّغ `unverified`، لأن `unshare --net` قد
  يفشل حسب المضيف؛ النتيجة الحقيقية تُسجّل لكل تنفيذ على حدة.
- **القراءة فقط**: `tests/test_platform_info.py` يتحقق أن مسار OpenAPI يحتوي `get`
  وحده، وأن كل توكنات الاختبار غائبة عن الاستجابة.

## كيف تُملأ بيانات الصورة

الصورة تقرأ ثلاث قيم وقت البناء:

| المتغيّر | المصدر | الأثر عند الغياب |
|---|---|---|
| `CELA_IMAGE_REF` | `--build-arg CELA_IMAGE_REF=ghcr.io/owner/repo:sha-…` | `configured: false` |
| `CELA_BUILD_SHA` | `--build-arg CELA_BUILD_SHA=$GITHUB_SHA` | `build_sha: null` |
| `CELA_BUILD_TIME` | `--build-arg CELA_BUILD_TIME=<ISO-8601 UTC>` | `build_time: null` |
| `CELA_IMAGE_TAG` | اختياري: وسم صريح إن لم يكن داخل المرجع | يُشتق من المرجع أو يبقى `null` |
| `CELA_IMAGE_DIGEST` | اختياري: `sha256:<64 hex>` لتثبيت البايتات | `digest: null` |

`Dockerfile` و`deploy/g1a/Dockerfile` يحملان `ARG` بقيمة فارغة افتراضيًا، و
`.github/workflows/docker-image.yml` يمرّرها في خطوة البناء. لذلك:

- صورة بُنيت عبر الـ workflow تعرض مرجعها وبصمة بناءها.
- `docker build .` محليًا بلا وسوم تعرض «غير مضبوط» بصدق.

سياسة النشر الحالية: دفعات `main` فقط تحدّث `:latest` و`:edge`، ووسوم الإصدار
`v*` تنشر وسم الإصدار نفسه؛ كلا المسارين ينشران أيضًا `:sha-<commit>`. فروع
`arena/**` ما زالت تبني الصورة وتختبرها، لكنها لا تسجّل الدخول إلى GHCR ولا تدفع
أي وسم. المرجع المحقون هو `:sha-<commit>` لأن هذا ما بُني فعلًا — لا ندّعي أن
الوسمين المتحركين `:latest` و`:edge` هما البايتات نفسها بعد دفع لاحق.

## الاختبارات

```sh
# بايثون: عقد النقطة والتحقّق من عدم تسرّب الأسرار
.venv/bin/pytest -q tests/test_platform_info.py

# الواجهة: تحليل العقود والعرض والتنسيق
npm --prefix packages/web run test:platform-console

# الفحوص المعتادة
npm --prefix packages/web run lint
npm --prefix packages/web run typecheck
npm --prefix packages/web run build
```

`tests/web/platform-console.cjs` يتحقق من: قبول حمولة كاملة، رفض حمولات ناقصة أو
مضللة (`status: degraded`، خصائص مفقودة)، بقاء القيم الغائبة `null` لا نصوصًا
مختلقة، رفض تقرير سجل تالف، دوال التنسيق، وتعطيل زر النسخ عندما لا توجد قيمة.

## الحدود — NOT VERIFIED

- **لم يُنشر** أي شيء في هذه الجلسة، ولم يُسحب وسم من GHCR ولم يُشغَّل حاوية.
- صلاحية وصول حزمة GHCR (عامة/خاصة) لا تُتحقق من الواجهة؛ تُثبتها عملية سحب ناجحة.
- «مدة التشغيل» تقدير من زمن استيراد وحدة الخادم، لا من مشرف العملية.
- عزل الشبكة في الوضع المحلي **غير متحقق**، ويُعرض بهذا اللفظ.
- لا تحقق مستقل من المستخدم، ولا مراجعة وصول شاملة، ولا دعم متعدد المستخدمين.
- Devbox لا يوفّر بيئة تطوير تفاعلية: هذا قرار نطاق صريح، وتنفيذه يحتاج خدمة عزل
  خارجية (E2B/Firecracker) وعقد صلاحيات جديدًا.
