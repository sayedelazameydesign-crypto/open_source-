# تكامل AI Indigo الخارجي (`INDIGO_API_KEY`) — التوصيف المعماري ودليل التشغيل

> **المرجع المعماري:** [ADR-0021](../adr/0021-indigo-api-integration.md) ·
> **العميل والموجّه:** [`packages/os_server/os_server/indigo_api.py`](../packages/os_server/os_server/indigo_api.py) ·
> **فاحص التحقق:** [`scripts/verify_indigo_api.py`](../scripts/verify_indigo_api.py)

---

## 1. الخلاصة التأسيسية

| البند | الحقيقة الهندسية في هذا المستودع |
|---|---|
| ما هو `INDIGO_API_KEY`؟ | مفتاح مصادقة خارجي لخدمة **AI Indigo REST API v1** (`https://aiindigo.com/api/v1`) يُرسَل في ترويسة `X-API-Key` |
| هل تحتاجه نواة النظام أو بوابات C5/C6؟ | **لا.** تشغيل `os_server` والنشر على Render (C5) واستمرارية PostgreSQL/Supabase (C6) مستقلة تمامًا عنه |
| ماذا يحدث إن لم يُضبط `INDIGO_API_KEY`؟ | يعمل النظام الأساسي واختبارات CI بشكل طبيعي 100%؛ وتبقى مسارات `/api/indigo/*` معطّلة (`503`) بلا أي أثر جانبي |
| متى يُفعَّل؟ | فقط عند ضبط `CELA_INDIGO_ENABLE=1` وتوفير `INDIGO_API_KEY` في بيئة الخادم أو في GitHub Actions Secrets |

---

## 2. المسارات المتاحة عند التفعيل (`CELA_INDIGO_ENABLE=1`)

تُحمى المسارات بنفس نظام المصادقة الموحّد في الخادم دون اختراع طبقة مصادقة ثانية:
- المساحة العامة: `/api/indigo/*` (تتطلب `Authorization: Bearer $CELA_API_TOKEN`)
- المساحة الخاصة: `/cela/v1/indigo/*` (تتطلب `Authorization: Bearer $CELA_PRIVATE_API_TOKEN`)

| المسار | يتطلب `INDIGO_API_KEY`؟ | الوظيفة |
|---|---|---|
| `GET /status` | لا | فحص حالة التفعيل واسم متغير البيئة ووجود المفتاح (`configured: bool`) دون كشف قيمته |
| `GET /trending` | لا (مسار عام في المصدر) | الأدوات الصاعدة خلال آخر 7 أيام (`GET /api/v1/intelligence/trending`) |
| `GET /search?q=...` | نعم | البحث الكلمي أو الدلالي (`keyword` / `semantic` / `hybrid`) مع إرجاع بيانات `rate_limit` |
| `GET /tools` | نعم | استعراض الأدوات مع التصفية (`category`, `pricing`, `sort`, `page`, `per_page`) |
| `GET /tools/compare?a=...&b=...` | نعم | مقارنة أداتين جنبًا إلى جنب |
| `GET /tools/{slug}` | نعم | تفاصيل أداة مفردة |
| `GET /categories` | نعم | قائمة التصنيفات وأعداد الأدوات |
| `GET /categories/{slug}` | نعم | تفاصيل تصنيف وأبرز أدواته |

---

## 3. الضمانات الأمنية المطبَّقة

1. **الوجهة المثبّتة (`EgressPolicy` + `SSRFGuard`):** يسمح `default_indigo_policy()` حصريًا بالمضيف `aiindigo.com` عبر `https` والمنفذ `443`، مع منع تتبع التحويلات (`follow_redirects=False`).
2. **حجب الأسرار (`os_core.redact` + `scrub_indigo_secret`):** تُحجب جميع المفاتيح من نمط `ai_live_...` و`aik_live_...` وأي قيمة حرفية لـ `INDIGO_API_KEY` من كافة السجلات والاستثناءات و`repr()`.
3. **الفحص الاستباقي في `deploy_doctor.py`:** لا يصدر أي تحذير في الوضع الافتراضي، ويُنبّه بالمعرّف `indigo_api_key_unset` فقط إذا فعّل المشغّل `CELA_INDIGO_ENABLE=1` ونسي ضبط `INDIGO_API_KEY`.

---

## 4. أوامر التحقق (المحلي وعبر GitHub Actions)

### أ) الفحص المحلي بدون شبكة (Offline)
```bash
python scripts/verify_indigo_api.py --offline-only
```

### ب) الفحص الحي عبر GitHub Actions (دون لصق المفتاح في المحادثة أو الطرفية)
1. أضف السر `INDIGO_API_KEY` في إعدادات المستودع:
   `Settings → Secrets and variables → Actions → New repository secret`
2. شغّل Workflow التحقق الحي `.github/workflows/indigo-live-check.yml`:
   - إما من تبويب **Actions → AI Indigo live check (INDIGO_API_KEY) → Run workflow**
   - أو عبر `gh workflow run indigo-live-check.yml` بعد دمج الفرع في `main`
3. تُنشر جميع المعايير السبعة مربوطة بـ SHA الالتزام مباشرةً كـ Check Annotations في صفحة الفحص.
