# ADR-0005: عزل الإنتاج Fail-Closed ثم واجهة Next.js 15

**التاريخ:** 2026-10-14 · **الحالة:** مقبول ومُنفَّذ (الخطوتان 2 و3)

## القرار — العزل أولًا
1. `E2BBackend` (SDK اختياري `cela-use-browser[sandbox]` + `E2B_API_KEY`) و`FirecrackerBackend`
   (عقد HTTP مع مدير MicroVM خارجي عبر `CELA_FIRECRACKER_ENDPOINT`) خلف بروتوكول
   `CodeExecBackend` نفسه — الوكلاء لا تتغير.
2. **Fail-Closed صارم:** غياب SDK/مفتاح/نقطة نهاية، تعذر الوصول للمدير، أو خطأ
   المزوّد ⇒ `ToolResult(status=DENIED, metadata.executed=False)`. لا يوجد أي مسار
   تراجع إلى `LocalSubprocessBackend` داخل هاتين الفئتين.
3. الاختيار: `CELA_CODE_EXEC_MODE=local|e2b|firecracker|production`؛ ووضع
   `production` دون `CELA_CODE_EXEC_BACKEND=e2b|firecracker` يرفض الإقلاع أصلًا.

## القرار — الواجهة
Next.js 15 (App Router) في `packages/web` بعرض مقسوم داكن: مسار تنفيذ حي +
Artifacts/توجيه نماذج. المتصفح **لا يرى أي رمز**: مسار `app/api/backend/*`
يخفي الرموز في بيئة خادم Next ويمرّر SSE كما هو، فيُحفَظ مؤشر `after_seq`
(يُخزَّن أيضًا في `localStorage` لكل تشغيل) وتُصفّى الأحداث المكررة بـ`seq`.
أزرار HITL تستدعي `/runs/{id}/approve` وعدم الرد يبقى رفضًا من جهة الخادم.

## حدود صادقة
* لا توجد خدمة E2B/Firecracker حية داخل بيئة التطوير هذه: ما مُختبَر هو عقد
  العميل (SDK وهمي، وHTTP بـ MockTransport) وسياسة الرفض — التشغيل الفعلي
  على MicroVM يحتاج مفتاح/مديرًا حقيقيين.
* الـ Artifacts الحالية بياناتها الوصفية فقط عبر الـ API القائم؛ عرض المحتوى
  الكامل يحتاج توسيع نقطة النهاية.
