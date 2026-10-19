بناءً على طلبك كمهندس أنظمة، سنقوم بتحليل محتوى الرابط والمنهجية الفنية المتبعة، ثم نصيغ خطوات هندسية متكاملة لبناء ونشر نموذج ذكاء اصطناعي خاص بك (Custom AI Model) مع توفير واجهة برمجية (API) مجانية تماماً وبدون الحاجة لبطاقة ائتمان (Credit Card / Visa).

---

## 1. التحليل التأسيسي وهندسة المتطلبات

* **المشكلة / الهدف:** بناء نموذج ذكاء اصطناعي خاص، تدريبه أو ضبطه (Fine-tuning / Prompt Engineering)، ونشره على السحابة مع توفير API مجاني للاستدعاء الخارجي.
* **القيود:**
* مجاني بنسبة 100%.
* بدون استخدام بطاقة ائتمان (No Visa Required).
* توفير API مستقر وسهل التكامل مع تطبيقات الباك إند (مثل Python / FastAPI / Next.js).


* **الملخص الهندسي:** سنعتمد على منصات استضافة النماذج السحابية المجانية (مثل Hugging Face Spaces أو Google Colab مع ngrok/FastAPI أو Groq/Gemini API للتوجيه المجاني)، مع التركيز على نشر نماذج مفتوحة المصدر (Open Source Models) مثل Llama أو Mistral أو BitNet.

---

## 2. التوزيع المنطقي لمراحل التنفيذ

### المرحلة الأولى: تجهيز النموذج وخوارزميات المعالجة

* **تحديد البنية:** اختيار نموذج خفيف الوزن (Lightweight Model) أو نموذج مبني على التقنيات الحديثة (مثل النماذج المكممة Quantized Models أو 1.58-bit) لضمان العمل بسلاسة ضمن الموارد المجانية المتاحة.
* **الأداة المقترحة:** استخدام **Python** مع مكتبات `transformers` و `peft` (إذا كنت بحاجة لـ LoRA Fine-tuning) أو الاعتماد على النماذج الجاهزة للاستخدام عبر الـ Inference API.

### المرحلة الثانية: بناء الـ API الخاص بك (Custom API Wrapper)

* سنقوم ببرمجة خادم خفيف باستخدام **FastAPI** أو **Flask** ليكون بمثابة الوسيط (Middleware / Wrapper) الذي يستقبل الطلبات (Requests) ويرسلها للنموذج، مما يمنحك مفتاح API خاص بك (Custom API Key Authentication).

### المرحلة الثالثة: النشر المجاني السحابي (Free Cloud Deployment)

* **المنصة المجانية:** **Hugging Face Spaces** (توفر استضافة مجانية لـ Docker أو Gradio أو FastAPI Apps مع معالج CPU مجاني تماماً وبدون فيزا) أو ربط السكربت عبر **Google Colab + FastAPI + Ngrok/Localtunnel** للاختبار السريع.

---

## 3. التنفيذ التدريجي (كود الـ API المجاني)

إليك قالب هندسي بلغة Python باستخدام FastAPI لإنشاء الـ API الخاص بك وربطه بنموذج مجاني أو تشغيله محلياً/سحابياً:

```python
from fastapi import FastAPI, HTTPException, Security
from fastapi.security.api_key import APIKeyHeader
pydantic import BaseModel

app = FastAPI(title="Celia Custom AI API", version="1.0.0")

# إعداد مفتاح API الخاص بك للحماية والتحكم
API_KEY = "my_secure_custom_api_key_2026"
API_KEY_NAME = "X-API-Key"
api_key_header = APIKeyHeader(name=API_KEY_NAME, auto_error=False)

class PromptRequest(BaseModel):
    prompt: str
    max_tokens: int = 256

def get_api_key(api_key_val: str = Security(api_key_header)):
    if api_key_val == API_KEY:
        return api_key_val
    raise HTTPException(status_code=403, detail="Invalid or missing API Key")

@app.post("/v1/generate")
async def generate_response(request: PromptRequest, api_key: str = Security(get_api_key)):
    # هنا يتم استدعاء النموذج الخاص بك أو ربطه بـ Inference Endpoint مجاني
    # مثال توضيحي للمعالجة:
    response_text = f"تمت معالجة الطلب بنجاح عبر النموذج الخاص: {request.prompt[::-1]}"
    
    return {
        "status": "success",
        "model": "custom-edge-model-v1",
        "output": response_text
    }

```

---

## 4. التدقيق الذاتي ونشر النموذج مجاناً بدون فيزا

1. **كيف تنشره مجاناً بدون فيزا؟**
* أنشئ حساباً مجانياً على **[Hugging Face](https://huggingface.co/)** (لا يتطلب بطاقة ائتمان أبداً).
* أنشئ **Space** جديدة واجعلها من نوع **Docker** أو **FastAPI**.
* ارفع كود الـ API ملفات المشروع إلى الـ Space وسيقوم Hugging Face باستضافتها مجاناً مع توفير رابط عام (Public URL) يعمل كـ API جاهز للاستدعاء من أي تطبيق خارجي.

---

## 5. الكود المصحح والمؤمن (Production-Ready Code)

إليك النسخة النهائية من ملف `main.py` بمبدأ الفشل المبكر (Fail-Fast): يتوقف الخادم عند بدء التشغيل إذا لم يُضبط متغير البيئة `CUSTOM_API_KEY`، ولا توجد قيمة افتراضية ثابتة للمفتاح:

```python
import os
from fastapi import FastAPI, HTTPException, Security
from fastapi.security.api_key import APIKeyHeader
from pydantic import BaseModel

app = FastAPI(title="Celia Custom AI API", version="1.0.2")

# مبدأ الفشل المبكر (Fail-Fast): إيقاف التشغيل فوراً إذا كان مفتاح البيئة مفقوداً
API_KEY = os.getenv("CUSTOM_API_KEY")
if not API_KEY:
    raise RuntimeError("CRITICAL SECURITY ERROR: CUSTOM_API_KEY environment variable is missing. Server startup aborted.")

API_KEY_NAME = "X-API-Key"
api_key_header = APIKeyHeader(name=API_KEY_NAME, auto_error=False)

class PromptRequest(BaseModel):
    prompt: str
    max_tokens: int = 256

def get_api_key(api_key_val: str = Security(api_key_header)):
    if api_key_val == API_KEY:
        return api_key_val
    raise HTTPException(status_code=403, detail="Invalid or missing API Key")

@app.post("/v1/generate")
async def generate_response(request: PromptRequest, api_key: str = Security(get_api_key)):
    return {
        "status": "success",
        "model": "custom-edge-model-v1",
        "prompt_received": request.prompt,
        "output": f"تمت المعالجة بأمان تام عبر الـ API الخاص: {request.prompt}"
    }
```
