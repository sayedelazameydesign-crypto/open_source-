# `scripts/` — أدوات التحقق والتشغيل

هذه السكربتات **ليست اختبارات وحدة**؛ إنها أدوات تشغيل يدوي/CI تتحقق من أشياء لا تستطيع
مجموعة `pytest` الافتراضية رؤيتها (شبكة حقيقية، قاعدة بيانات حقيقية، خدمة منشورة، ملف
وسم التوكنز). لذلك لا تُجمَع بواسطة `pytest` (فهي خارج `tests/`)، و`make test` لا يشغّلها.

**القاعدة:** لا سكربت هنا يُعدّل حالة إنتاج. كلها تقرأ وتتحقق أو تطبع تقريرًا؛ ما عدا
`defork_launcher.sh` الذي يعدّل شجرة محلية (والاستخدام مقصود لعمليات الـfork).

## مرتبطة بـ CI أو `make`

| السكربت | ماذا يفعل | من يشغّله |
|---|---|---|
| `update_evidence.py` | يولّد — أو يتحقق بـ`--check` — كتلة عدد الاختبارات في `docs/EVIDENCE.md`. | `ci.yml` (فشل عند الانحراف) · `make evidence` / `make evidence-check` |
| `vendored_manifest.py` | بيان نزاهة لقطة `packages/craft-launcher` (`verify`، و`--upstream` يعيد اشتقاقه من tarball مثبّت). | `launcher-ci.yml` · `make vendored-verify` |
| `deploy_doctor.py` | فحص جاهزية النشر (`--strict`): أسرار، عناوين، مخزن دائم، `CELA_CODE_EXEC_MODE=disabled`، Blueprint الهدف. | `deploy-verify.yml` · `make` لا يشغّله تلقائيًا |
| `verify_deployment.py` | تحقق بعد النشر من خدمة منشورة (صحة + هوية التنفيذ)، بقائمة سماح صريحة للمضيفين. | `make verify-deploy` |
| `verify_indigo_api.py` | يتحقق من تكامل Indigo بلا شبكة، ثم حيًّا عند السماح (مع `--allow-host`). | `indigo-live-check.yml` |
| `publish_acceptance_annotations.py` | ينشر حكم A1/A6 كتعليقات فحص GitHub من سجل التشغيل. | `live-acceptance.yml` · `indigo-live-check.yml` |
| `test_gemini_provider.py` | قبول A1/A6 لمزوّد Gemini حقيقي (يحتاج مفتاحًا). | `live-acceptance.yml` (يدوي) |
| `defork_launcher.sh` | يزيل علامات ArtCraft من نسخة الـfork — تشغيل واعٍ ومقصود فقط. | يدوي (fork) |

## أدوات يدوية بلا حمّلة CI

هذه الأدوات **صالحة ومقصودة**، لكن لا حمّلة عمل تستدعيها اليوم؛ تُشغَّل يدويًا عند التحقق
من الاستمرارية أو شكل الطلب. أُبقيت موثّقة بدل حذفها (فالحذف يفقد دليلًا لا ينتجه أي اختبار
آخر)، وكُتب هذا الملف ليكون مكانها الوحيد في الوثائق:

| السكربت | ماذا يفعل | المتطلبات |
|---|---|---|
| `test_durable_store.py` | دخان C6: مخزن التشغيل الدائم ينجو من إعادة تشغيل محاكاة. | بلا شبكة |
| `test_postgres_persistence.py` | دخان C6 مقابل PostgreSQL حقيقي عبر إعادات اتصال. | `CELA_TEST_POSTGRES_DSN` + extra `distributed` |
| `test_cli_gemini_request_shape.py` | يتحقق من شكل الطلب المرسل إلى Gemini مقابل ناقل موكَّب (بلا شبكة، بلا مفتاح). | بلا مفاتيح |
| `acceptance_artifacts.py` | فحص مشترك لمخرجات سكربتات القبول (يستورده ما سبق). | — |

## تحذير تسمية

أسماء ثلاثة منها تبدأ بـ`test_` لأنها "اختبارات قبول"، لكنها **ليست** جزءًا من مجموعة
`pytest`: لا تُجمَع، ولا تُشغَّل في CI، ولا تُحتسب في `docs/EVIDENCE.md`. لا تعِد تسميتها بلا تحديث
`docs/DEPLOYMENT_RUNBOOK.md` و`docs/DEPLOY_FREE_TIER_RUNBOOK.md` (تشيران إلى
`test_postgres_persistence.py` و`test_gemini_provider.py` صراحةً).
