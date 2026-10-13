# `cela_use_browser` Release — `v0.1.0`

We are pleased to announce **`v0.1.0`** of **`cela_use_browser` (`open_source-`)** — an autonomous browser, sandboxed code execution, and offline CPU AI platform built on a 100% open-source stack.

---

## Highlights in `v0.1.0`

### 1. Dual-Mode Agent Orchestration (`os_orchestration` & `os_agents`)
- **Direct Agent Mode (`ReActAgent`):** Step-by-step `Reason → Act → Observe` execution loop with stagnation fingerprinting and automated trajectory review (`HeuristicReviewer` / `ModelReviewer`).
- **Flow Orchestration Mode (`PlanningFlow`):** Decomposes complex tasks into a validated Directed Acyclic Graph (**DAG**), runs independent branches concurrently (`asyncio.gather`), interpolates cross-step outputs (`{{s1.output}}`), and performs dynamic re-planning on step failure.
- **Adaptive Model Routing (`ModelRouter`):** Profiles every request and routes across `planner`, `fast`, `vision`, and `critic` tiers under a hard token budget with automatic planner-to-fast fallback.

### 2. Sandboxed Execution & Resilient Browser Automation (`os_tools`)
- **Sandboxed Code Interpreter (`LocalSubprocessBackend`):** Executes untrusted Python in a child process with POSIX `setrlimit` ceilings (CPU, address space, file size, and `RLIMIT_NPROC` fork-bomb protection), wall-clock timeouts, an allow-listed environment that prevents parent secret leakage (#1), and `unshare --net` network namespace isolation when available, collecting generated files as live `Artifacts`.
- **3-Tier Browser Fallback (`BrowserTool`):** Survives DOM mutations via: (1) selector retry with backoff, (2) accessible-text relocation (`text=...`), and (3) VLM screenshot coordinate fallback.

### 3. Offline CPU AI Engine (`LocalAIEngine` — #4)
- **Bilingual Arabic/English NLP:** Arabic normalisation, language detection (`ar` / `en` / `mixed`), and extraction of 9 entity types (`PERSON`, `LOCATION`, `ORG`, `DATE`, `MONEY`, `PERCENT`, `EMAIL`, `URL`, `PHONE`) plus keywords.
- **Semantic Search (`semantic_search`):** Hybrid retrieval combining Arabic/English light morphological stemming, TF-IDF, character 3/4-grams, and bilingual concept clusters.
- **Classical ML (`ml_analyze`):** Standard-scaled `k-NN` classification, OLS linear regression forecasting, deterministic `k-means++` clustering, and `MAD Z-score` anomaly detection.
- **Vision, Document OCR & Audio:** Binary inspection of `PNG`/`JPEG`/`GIF`/`BMP`/`PPM`, text/field extraction from `PDF`, `PNG`, and `HTML`/`SVG` (`document_ocr`), and `RIFF/WAVE` signal metrics (`audio_transcribe`), exposed as MCP tools, `CelaBrowser` methods, and `/api/*` + `/cela/v1/*` routes.

### 4. Security, Governance & Dual-Surface API (`os_core` & `os_server`)
- **SSRF Floor & Explicit Egress Policy (`EgressPolicy` — #3):** Blocks loopback, RFC1918, link-local cloud metadata (`169.254.169.254`), and DNS rebinding attacks, enforcing a default-deny allow-list over `SSRFGuard`.
- **Bilingual Guardrails & Fail-Closed HITL:** Screens user prompts and tool arguments against 7 English/Arabic prompt-injection patterns and gates `DANGEROUS` actions on human approval (`QueueApprover` fails closed on timeout).
- **Isolated API Surfaces (#2, #3):**
  - Public platform routes (`/runs`, `/ws/{id}`, `/runs/{id}/ws-ticket`, …) protected by `CELA_API_TOKEN` / `OS_API_TOKEN` with single-use run-scoped WebSocket tickets.
  - Private namespace (`/cela/v1/*`) protected by a separate `CELA_PRIVATE_API_TOKEN` with pre-flight egress enforcement before any run is created, paired with the in-process `CelaBrowser` Python facade.

---

## Included Pull Requests
- **#1:** `fix(sandbox): prevent parent environment secret leakage`
- **#2:** `feat(api): add authentication to run endpoints`
- **#3:** `feat(cela): add cela_use_browser private API, CelaBrowser facade, egress policy and WS tickets`
- **#4:** `feat(ai): add offline CPU LocalAIEngine, MCP tools, CelaBrowser methods, and /api + /cela/v1 routes`

## Verification
- **345 passing tests** (`pytest -q`), 100% offline and deterministic with zero external API keys required.
- Verified across Python 3.11, 3.12, and 3.13 with clean `ruff check` and `ruff format` checks.
