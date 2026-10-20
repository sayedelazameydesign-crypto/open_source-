# ADR-0021: عزل تكامل AI Indigo الخارجي (`INDIGO_API_KEY`) والتحقق الحي عبر Actions

**التاريخ:** 2026-10-19 · **الحالة:** مقبول ومُنفَّذ

## السياق

ظهر التساؤل حول طبيعة المتغير `INDIGO_API_KEY` وعلاقته بنواة المشروع وبوابتي
النشر C5 (Render) وC6 (PostgreSQL / Supabase):

1. `INDIGO_API_KEY` هو مفتاح مصادقة خارجي خاص بمنصة **AI Indigo**
   (`https://aiindigo.com/api/v1` — توثيق المطورين على `/developers/docs`)، يُمرَّر
   في ترويسة `X-API-Key` للوصول إلى فهرس الأدوات والبحث والتصنيفات.
2. نواة المنصة (`os_server`، `render.yaml`، `deploy_doctor.py`، اختبارات C5/C6)
   تعتمد حصريًا على متغيراتها الخاصة (`GEMINI_API_KEY`، `CELA_API_TOKEN`،
   `CELA_PRIVATE_API_TOKEN`، `CELA_RUN_STORE_URL`) **ولا تتطلب `INDIGO_API_KEY`**.
3. عند الرغبة في ربط النظام بخدمات AI Indigo أو التحقق الحي من صلاحية المفتاح
   المحفوظ في GitHub Actions Secrets دون كشف قيمته في الطرفية أو السجلات، يلزم
   وجود طبقة معزولة اختيارية تتبع نفس الضمانات الأمنية المعتمدة في ADR-0011 وADR-0013.

## القرار

1. **فصل تام واختياري (`CELA_INDIGO_ENABLE=1`):**
   - يُنفَّذ العميل والموجّه في `packages/os_server/os_server/indigo_api.py` تحت
     المسارين `/api/indigo/*` (برمز المنصة العام) و`/cela/v1/indigo/*` (برمز المسار الخاص).
   - افتراضيًا `CELA_INDIGO_ENABLE=0`: يبدأ `create_app()` وتمرّ فحوص `deploy_doctor.py`
     واختبارات CI بالكامل دون أي حاجة لـ `INDIGO_API_KEY`.
   - عند التفعيل مع غياب المفتاح تفشل المسارات المحمية مغلقةً برمز `503`
     (`indigo_unconfigured`) **قبل فتح أي مقبس شبكي**.
2. **تثبيت الوجهة وحماية الأسرار:**
   - تقيد سياسة الخروج `default_indigo_policy()` الاتصال بـ `https://aiindigo.com`
     على المنفذ `443` فوق حارس `SSRFGuard` مع تعطيل تتبع التحويلات
     (`follow_redirects=False`) منعًا لتسريب ترويسة `X-API-Key` عبر تحويل HTTP.
   - أُضيفت بادئات مفاتيح AI Indigo (`ai_live_...` و`aik_live_...`) إلى
     `os_core.redact._KEY_SHAPE_RE` ليُحجب المفتاح تلقائيًا في كل طبقات النظام،
     بالإضافة إلى المسح الحرفي لقيمة السر النشط في `scrub_indigo_secret`.
3. **التحقق الحي بلا كشف السر (`.github/workflows/indigo-live-check.yml`):**
   - يحقن السر `${{ secrets.INDIGO_API_KEY }}` في بيئة خطوة التشغيل وحدها نحو
     الوجهة المثبّتة `https://aiindigo.com/api/v1` عبر `scripts/verify_indigo_api.py`،
     وينشر نتائج الفحوص المربوطة بـ SHA الالتزام كـ Check Annotations مع حارس أمني
     آلي في `tests/test_indigo_workflow_security.py`.
