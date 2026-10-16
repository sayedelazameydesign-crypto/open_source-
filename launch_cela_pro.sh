#!/usr/bin/env bash
# launch_cela_pro.sh — تشغيل CELA.PRO (Gradio) مع رابط عام (share=True)
set -euo pipefail

echo "=== CELA.PRO launcher ==="

# 1) تثبيت Gradio إن لم يكن موجودًا
if ! python -c "import gradio" 2>/dev/null; then
    echo "[*] تثبيت Gradio..."
    pip install --quiet --upgrade gradio httpx
fi
if ! python -c "import pypdf" 2>/dev/null; then
    echo "[*] تثبيت قارئ PDF الاختياري..."
    pip install --quiet --upgrade pypdf
fi

# 2) متغيرات اختيارية
export KIMI_API_KEY="${KIMI_API_KEY:-}"
export KIMI_BASE_URL="${KIMI_BASE_URL:-https://api.moonshot.cn/v1}"
export KIMI_MODEL="${KIMI_MODEL:-moonshot-v1-8k}"

echo "[*] KIMI_API_KEY: $([ -n "$KIMI_API_KEY" ] && echo 'مُعيّن' || echo 'غير مُعيّن (وضع تجريبي)')"
echo "[*] تشغيل CELA.PRO على 0.0.0.0:7860 (share=True)..."

python cela_pro_app.py
