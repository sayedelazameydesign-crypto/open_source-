# ADR-0011: ربط نموذج خاص عبر أسرار CI دون كشفها

**التاريخ:** 2026-10-14 · **الحالة:** مقبول ومُنفَّذ

## السياق
المفاتيح (`APIDOG_ACCESS_TOKEN` وغيرها) محفوظة GitHub Repository Secrets.
قيم الأسرار لا تصل إلى جلسات التطوير ولا يجب أن تُطبع أو تُمرَّر كوسائط CLI.

## القرار
1. `OpenAICompatModel` يقرأ المفتاح عند الطلب من متغير بيئة مسمّى
   (`api_key_env`)؛ لا يُخزَّن على الكائن ولا يدخل أي تسلسل (أُثبت باختبار
   أن القيمة غائبة من `model_dump_json`). أسلوبا ترويسة: `Authorization: Bearer`
   أو `x-api-key` لبوابات ApiDog-style.
2. `os_evals` يقبل `--api-key-env <NAME>` و`--auth-header`؛ قيمة المفتاح
   نفسها ليست وسيطًا في أي مكان.
3. Workflow يدوي فقط (`.github/workflows/eval-real-model.yml`،
   `workflow_dispatch` بمدخلات `model_url`/`model_id` غير السرية) يحقن
   `secrets.APIDOG_ACCESS_TOKEN` في `CELA_EVAL_API_KEY` داخل خطوة التشغيل
   وحدها، ويشغّل A/B بالمهارات على النموذج الحقيقي ويرفع التقرير Artifact.
   أسرار GitHub لا تُمرَّر لـ fork PRs بتصميم GitHub نفسه.

## ما ينقص للتشغيل الفعلي
قيمتان غير سريتين من مالك الـAPI: `model_url` و`model_id` (يُدخلان عند
تشغيل الـWorkflow يدويًا أو كـ Repository Variables). دون معرفة شكل
الاستجابة غير المتوافق مع OpenAI لا يوجد «محول سحري» — أي اختلاف يُنفَّذ
كمحوّل صريح بعد رؤية توثيق عام.
