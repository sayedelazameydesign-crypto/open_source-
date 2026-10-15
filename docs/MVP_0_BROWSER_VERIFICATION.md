# CELIA MVP-0 — تحقق المتصفح وعرض الهاتف

## STATUS → PASS ضمن النطاق المحلي المحدد

اجتازت الرحلة متصفح Chromium حقيقيًا **بوضع headless**، وليس مجرد HTTP أو Mock
للواجهة. اجتاز عرض الهاتف محاكاة `390×844`؛ **لم يُختبر هاتف مادي**.

- الفرع: `arena/b25f704f-open-source`؛ الالتزام الأساسي: `14db8a3`.
- لا تغيير في ملفات المنتج أو API أو المصادقة في هذه المرحلة؛ حُفظت تعديلات المرحلة السابقة.
- لم يحدث commit أو push أو merge أو نشر أو تغيير فوترة أو صلاحيات.
- البيانات اصطناعية فقط؛ SQLite مؤقتة؛ لا نموذج أو خدمة خارجية في الاختبار.

## RESULT — مصفوفة القبول

| البند | الحالة | الدليل |
|---|---|---|
| العربية وRTL على سطح المكتب | PASS | DOM ولقطة الخطة |
| رفع TXT وعرض ثلاث خطوات دون تنفيذ | PASS | تفاعل المتصفح + حالة AWAITING_CONFIRMATION بلا Artifact أو حدث RUNNING |
| النقر لتأكيد التنفيذ | PASS | ظهور اكتملت المعالجة بعد النقر فقط |
| تنزيل فعلي | PASS | انتظار حدث download قبل النقر، ثم saveAs وقراءة بايتات الملف الذي نزّله Chromium |
| إحصاءات النص والمقتطف وSHA-256 | PASS | فحص بايتات التقرير ومقارنتها ببيانات Artifact، على سطح المكتب والهاتف |
| إعادة التحميل والاسترجاع | PASS | إدخال المعرّف واسترجاع المهمة من الواجهة بعد reload |
| عدم تكرار التقرير | PASS | إعادة إرسال طلبَي تنفيذ متزامنين تعيد نفس Artifact وحدث RUNNING واحدًا |
| ملف أكبر من 1 MiB | PASS | رسالة حجم واضحة في الواجهة دون إنشاء مهمة |
| UTF-8 غير صالح / امتداد مرفوض | PASS | الواجهة تعرض رفض الخادم 422 / 415 |
| غياب المصادقة / مهمة أو Artifact مفقود | PASS | طلبات API محلية فعلية تعيد 401 / 404 |
| فشل إنشاء التقرير | PASS | عطل محقون في fixture الخادم فقط؛ حالة FAILED ورسالة فشل بلا رابط تنزيل أو نجاح زائف |
| محاكاة الهاتف 390×844 | PASS | دورة رفع/خطة/تأكيد/تنزيل مستقلة |
| عدم التمرير الأفقي أو تداخل الضوابط | PASS | scrollWidth = innerWidth = 390؛ لا عناصر تحكم خارج العرض أو متداخلة |
| تنقل لوحة المفاتيح | PASS | Tab من الملف للرفع ثم حقل الاسترجاع وزره ثم التنفيذ؛ Enter ينفذ؛ رابط التنزيل قابل للتركيز |
| رسالة الخطأ على الهاتف | PASS | ظاهرة داخل viewport بعد التمرير إليها، بلا تجاوز أفقي |
| أخطاء JavaScript أو HTTP غير المتوقعة | PASS | صفر pageErrors وrequestFailures، ولا أخطاء شبكة خارج سيناريوهات الرفض المتوقعة |
| عدم تسريب الرمز/المحتوى في السجلات | PASS محدود | فحص console وطلبات المتصفح ومسح سجلي الخادمين مقابل الرمز المؤقت ونصوص fixture |
| عدم التراجع في `/runs` والتخزين | PASS | 64 اختبارًا مستهدفًا؛ المجموعة الكاملة 935 passed و1 skipped |
| هاتف مادي، Safari أو Firefox | NOT VERIFIED | خارج هذه الجولة |
| تدقيق وصول شامل أو أمان إنتاجي | NOT VERIFIED | لا يُستنتج من هذه الفحوص المحدودة |

الفحص المتزامن خاص بطلبات التنفيذ داخل عملية واحدة، وليس اختبار ضغط أو عدة workers.
رفع الملف تم عبر `setInputFiles`؛ لم يُختبر حوار اختيار ملفات نظام تشغيل هاتف حقيقي.
المشاهدات المرئية منفصلة عن الاختبارات: روجعت لقطتا الخطة والنجاح، والنص العربي مقروء
ولا يظهر تداخل. لقطة الهاتف full-page أطول قليلًا من viewport؛ التمرير الرأسي متوقع.

## EVIDENCE — الأدلة المحفوظة

الأدلة المنقّحة في [`test-evidence/mvp0-browser/`](test-evidence/mvp0-browser/):

- [حالة الخطة قبل التنفيذ](test-evidence/mvp0-browser/desktop-plan.png).
- [نجاح المهمة بمحاكاة الهاتف](test-evidence/mvp0-browser/mobile-success.png).
- [نتائج المتصفح وأسماء التنزيلات وبصماتها](test-evidence/mvp0-browser/browser-results.json).
- [مخرجات Playwright النهائية](test-evidence/mvp0-browser/playwright.log): **10 PASS، صفر FAIL**.
- [اختبارات عدم التراجع المستهدفة](test-evidence/mvp0-browser/pytest-targeted.log): **64 passed**.
- [المجموعة الكاملة](test-evidence/mvp0-browser/pytest-full.log): **935 passed، 1 skipped**.
- [Lint](test-evidence/mvp0-browser/ruff.log): **All checks passed**.

الاختبار المتخطى من مجموعة Postgres، والاعتماد الاختياري psycopg غير متاح؛ لا يتعلق
بالمستندات. يظهر تحذير deprecation من Starlette/httpx في المجموعة المستهدفة دون فشل.

سجل المتصفح يحتوي استجابتَي 422 و415 ورسالتَي console خاصتين بهما أثناء اختبار الرفض؛
هذه **متوقعة** وليست صفر أخطاء مطلقًا. لا اتصالات صفحة خارج الأصل المحلي ولا طلبات فاشلة.
طلبات التحقق المباشرة من المصادقة و404 تنفّذها APIRequestContext وليست متصفحًا غير موثوق
يحصل على الرمز. لم يظهر رمز المنصة ضمن ترويسات طلبات الصفحة نفسها.

فحص السجلات مقارنة دقيقة مع بيانات الاختبار المعروفة، وليس برهانًا عامًا على منع كل
أنواع تسريب المعلومات. السجلات الخام وقاعدة SQLite والتقارير المنزلة والرمز المؤقت
حُذفت بعد حفظ اللقطات والنتائج المنقّحة؛ خادما الاختبار أُوقفا وفُحص إغلاق المنفذين.

## بيئة الاختبار والملفات المضافة

- Playwright **1.64.1** وChromium **153.0.8010.0** من `@sparticuz/chromium@153.0.0`.
- لم يكن Playwright أو متصفح مثبتًا مسبقًا. ثُبتت أدوات الاختبار مؤقتًا تحت `.cache/`
  من npm، دون تعديل package.json أو lockfile أو اعتماديات الإنتاج.
- استُخدمت مكتبات Linux المرفقة بالحزمة دون تثبيت حزم نظام. المتصفح يعمل بلا GPU
  وبلا OS sandbox في هذه البيئة المعزولة؛ لا يُعد ذلك اختبارًا لعزل كود غير موثوق.
  لم يُستخدم `--disable-web-security` في الجولة النهائية.
- `tests/browser/document-runs.mjs`: رحلة المتصفح والتحقق والتنزيل والهندسة والسجلات المنقّحة.
- `tests/browser/document_server.py`: fixture محلي مع موافقة بيئية صريحة، وخطأ اصطناعي
  واحد لاختبار الفشل. ليس جزءًا من الخادم الإنتاجي؛ لا ينشئ نموذجًا أو منفّذ أدوات.

أثناء تجهيز الاختبار عولج غياب مكتبات Chromium باستخدام المكتبات المرفقة، وأوقف GPU
بعد فشل تشغيل عابر. صُحح محدّد alert في الاختبار ليتجاهل Next route announcer؛ لم تُغيَّر
واجهة المنتج لتمرير الاختبار. أعيدت الجولة كاملة بالإعداد النهائي فاجتازت البنود العشرة.

## إعادة الإنتاج محليًا

نفّذ من جذر المستودع؛ تحتاج Node وبيئة Python واعتماديات المشروع الحالية وNext build.
تنزيل أدوات الاختبار خطوة تجهيز فقط؛ مسار الاختبار ذاته محلي، ولا يستعمل AI.

```sh
mkdir -p .cache/mvp0-browser/results
npm install --prefix .cache/mvp0-browser --no-save --no-package-lock --no-audit --no-fund \
  playwright@1.64.1 @sparticuz/chromium@153.0.0
.venv/bin/python - <<'PY'
import os, secrets
fd = os.open('.cache/mvp0-browser/token', os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, 'w') as file:
    file.write(secrets.token_urlsafe(32))
PY
# عند الحاجة لبناء الواجهة، من packages/web:
# NEXT_TELEMETRY_DISABLED=1 npm run build
```

شغّل خادم الاختبار في طرفية مستقلة (لا تستخدمه للإنتاج):

```sh
CELA_E2E_TEST_MODE=1 \
CELA_API_TOKEN="$(cat .cache/mvp0-browser/token)" \
CELA_LEDGER_PATH=off \
CELA_RUN_STORE_URL=sqlite:///.cache/mvp0-browser/results/runs.db \
.venv/bin/python tests/browser/document_server.py \
  > .cache/mvp0-browser/results/api.log 2>&1
```

وطرفية ثانية، من `packages/web`:

```sh
NEXT_TELEMETRY_DISABLED=1 \
CELA_API_TOKEN="$(cat ../../.cache/mvp0-browser/token)" \
CELA_BACKEND_URL=http://127.0.0.1:8080 \
npm run start -- --hostname 0.0.0.0 --port 3000 \
  > ../../.cache/mvp0-browser/results/web.log 2>&1
```

بعد جاهزية الخادمين، من الجذر:

```sh
node tests/browser/document-runs.mjs
.venv/bin/pytest -q tests/test_documents.py tests/test_server.py tests/test_storage.py
.venv/bin/pytest -q
.venv/bin/ruff check .
```

هذه إعدادات محلية لمستخدم موثوق فقط؛ لا تعرض وكيل Next للإنترنت. أوقف الخادمين
بعد الاختبار، وافحص السجلات دون طباعة الرمز، ثم احذف مجلد `.cache/mvp0-browser`
بعد حفظ الأدلة المنقّحة المطلوبة. لا تحفظ SQLite أو الرمز أو الملفات الأصلية في Git.

## NEXT

المرحلة مغلقة ضمن Chromium المحلي ومحاكاة الهاتف. قبل PDF/OCR أو نماذج AI، حدد
سياسة الاحتفاظ بالمصدر والتقرير، والتصدير والحذف وحدود التخزين والنسخ الاحتياطية.
الحذف المنطقي ليس محوًا آمنًا من SQLite/WAL. دعم عدة عمليات خادم ما زال غير مثبت.
