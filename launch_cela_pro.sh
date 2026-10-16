#!/usr/bin/env bash
# launch_cela_pro.sh — تشغيل CELA.PRO (Gradio) مع رابط عام (share=True) وقائمة مزوّدي LLM
set -euo pipefail

echo "=== CELA.PRO launcher ==="

# 1) تثبيت Gradio و httpx إن لم يكونا موجودين
if ! python -c "import gradio, httpx" 2>/dev/null; then
    echo "[*] تثبيت Gradio و httpx..."
    pip install --quiet --upgrade gradio httpx
fi

# 2) إعدادات المزوّدين الافتراضية (دون كشف المفاتيح)
export CELA_DEFAULT_PROVIDER="${CELA_DEFAULT_PROVIDER:-Groq}"

export GROQ_API_KEY="${GROQ_API_KEY:-}"
export GROQ_BASE_URL="${GROQ_BASE_URL:-https://api.groq.com/openai/v1}"
export GROQ_MODEL="${GROQ_MODEL:-openai/gpt-oss-20b}"

export GEMINI_API_KEY="${GEMINI_API_KEY:-}"
export GEMINI_BASE_URL="${GEMINI_BASE_URL:-https://generativelanguage.googleapis.com/v1beta/openai}"
export GEMINI_MODEL="${GEMINI_MODEL:-gemini-2.5-flash}"

export OPENROUTER_API_KEY="${OPENROUTER_API_KEY:-}"
export OPENROUTER_BASE_URL="${OPENROUTER_BASE_URL:-https://openrouter.ai/api/v1}"
export OPENROUTER_MODEL="${OPENROUTER_MODEL:-openrouter/free}"

export KIMI_API_KEY="${KIMI_API_KEY:-}"
export KIMI_BASE_URL="${KIMI_BASE_URL:-https://api.moonshot.cn/v1}"
export KIMI_MODEL="${KIMI_MODEL:-moonshot-v1-8k}"

export OLLAMA_BASE_URL="${OLLAMA_BASE_URL:-http://localhost:11434/v1}"
export OLLAMA_MODEL="${OLLAMA_MODEL:-llama3.2:latest}"

echo "[*] المزوّد الافتراضي: ${CELA_DEFAULT_PROVIDER}"
echo "[*] GROQ_API_KEY: $([ -n "$GROQ_API_KEY" ] && echo 'مُعيّن' || echo 'غير مُعيّن')"
echo "[*] GEMINI_API_KEY: $([ -n "$GEMINI_API_KEY" ] && echo 'مُعيّن' || echo 'غير مُعيّن')"
echo "[*] OPENROUTER_API_KEY: $([ -n "$OPENROUTER_API_KEY" ] && echo 'مُعيّن' || echo 'غير مُعيّن')"
echo "[*] KIMI_API_KEY: $([ -n "$KIMI_API_KEY" ] && echo 'مُعيّن' || echo 'غير مُعيّن')"
echo "[*] Ollama: ${OLLAMA_BASE_URL} (${OLLAMA_MODEL})"

if [ -z "${GRADIO_USERNAME:-}" ] || [ -z "${GRADIO_PASSWORD:-}" ]; then
    echo "[!] تحذير: لم يُعيّن GRADIO_USERNAME و GRADIO_PASSWORD؛ الرابط العام سيكون متاحًا بدون كلمة مرور."
fi

echo "[*] تشغيل CELA.PRO على 0.0.0.0:7860 (share=True)..."

python cela_pro_app.py
