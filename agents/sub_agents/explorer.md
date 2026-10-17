# Explorer Sub-Agent Config
# المسار الأول من خطة الدمج: وكيل استكشاف خفيف محلي (Read & Grep فقط).
# يُشغَّل عبر Ollama أو أي نقطة نهاية متوافقة مع OpenAI محلياً — بلا تكاليف API.
name: "explorer"
model: "llama3:8b"
role: "Search and repository exploration"
allowed_tools: ["file_reader", "grep_search"]
required_ram_gb: 4
max_steps: 6
max_seconds: 120
