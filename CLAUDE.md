# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install dependencies
pip install -r requirement.txt

# Run once immediately
python src/main.py

# Run with options
python src/main.py --date 2025-01-15        # specify report date
python src/main.py --config path/to/cfg.yaml
python src/main.py --schedule               # daily scheduler mode (blocking)

# Generate/update the HTML dashboard
python src/utils/stat_db.py
python src/utils/stat_db.py --search "keyword" --export output.csv

# Re-run AI analysis on existing DB articles
python src/utils/reanalyze.py

# Run the web console (library + subscriptions + chat + tasks + settings)
PYTHONPATH="src:.deps" python src/web_server.py --config config/config.yaml --port 8080
```

All commands must be run from the repo root. Logs go to `data/logs/YYYY-MM-DD.log`.

## Configuration

Copy `config/config_template.yaml` to `config/config.yaml` (gitignored). Key fields:

- `llm.provider`: `"deepseek"` or `"qwen"` — uses OpenAI-compatible API
- `research_topics`: list of keyword strings used in LLM relevance scoring
- `relevance_threshold`: articles scoring below this (0–10) are filtered out
- `fetcher.use_fulltext`: set `false` to skip full-text fetching (abstract-only mode)
- `fetcher.use_browser`: set `false` to disable DrissionPage/Chromium browser rendering

API keys can be injected via env vars (`DEEPSEEK_API_KEY`, `QWEN_API_KEY`, `EMAIL_PASSWORD`, `FEISHU_WEBHOOK_URL`) — these override `config.yaml` values, which is the intended pattern for CI/server deployment.

## Architecture

The pipeline in `src/main.py:run_once()` runs 8 sequential steps:

1. **RSS fetch** (`core/fetcher.py:JournalFetcher.fetch_all`) — fetches all configured journal RSS feeds
2. **Dedup** (`core/db.py:Database.check_duplicate`) — skips articles already in SQLite by DOI, URL, or fuzzy title match
3. **LLM relevance filter** (`core/analyzer.py:LLMAnalyzer.filter_relevance`) — concurrent scoring via ThreadPoolExecutor; articles below `relevance_threshold` are dropped
4. **Full-text fetch** (`core/fetcher.py:JournalFetcher.fetch_fulltext_batch`) — concurrent; only runs on articles that passed step 3
5. **LLM deep analysis** (`core/analyzer.py:LLMAnalyzer.analyze_article`) — concurrent; skipped for abstract-only articles
6. **DB save** (`core/db.py:Database.save_article`)
7. **HTML index rebuild** (`utils/stat_db.py:build_html_index`) — regenerates `data/output/paper_index.html`
8. **Report + notify** (`core/notifier.py:Notifier.notify`) — writes markdown/HTML report, sends email and/or Feishu webhook

### Full-text fetch fallback chain (`core/fetcher.py:fetch_fulltext_with_status`)

For each article, tries in order:

1. Unpaywall OA link (free access)
2. Direct HTTP request via `RequestManager` with publisher-specific CSS selectors
3. Browser rendering via DrissionPage/Chromium (handles JS-heavy pages, Cloudflare)
4. OpenAlex abstract fallback

Publisher is identified from DOI prefix (`DOI_PUBLISHER_MAP`), not the RSS `publisher` field.

### Key shared types (`src/fetchers/models.py`)

`FetchResult` is the data contract between the fetcher and main pipeline. `EvidenceLevel` (`FULLTEXT` vs `ABSTRACT_ONLY`) controls whether deep LLM analysis runs — abstract-only articles get a truncated analysis template.

### Database (`core/db.py`)

SQLite with WAL mode. Thread-safe via thread-local connections for file DBs, or a shared connection + lock for `:memory:` mode. Dedup uses: exact DOI match → exact URL match → MD5 title hash → fuzzy title similarity (≥0.80 within 7 days).

Tables:

- `articles` — includes personal-library columns `starred` (0/1), `note` (text), `tags` (comma-separated) via ALTER TABLE migration
- `chat_messages` — per-article AI chat history (`article_id`, `role` user/assistant, `content`)
- `daily_reports`, `journals` — journal ids are renumbered 1..N after each delete (nothing references them externally; `articles.journal` stores the journal *name*, not the id)

### Web console (`src/web_server.py` + `src/static/`)

Standard-library `ThreadingHTTPServer`, no web framework. The frontend is vanilla JS static files under `src/static/` (index.html + css/style.css + js/*.js), served via `/static/*` (path-traversal-guarded, no-cache); `/` serves `index.html`. Views: 文献库 (search/star/tags/notes/pagination), 订阅管理, 历史日报 (iframe preview), 运行任务, 设置 (writes back to config.yaml via ruamel, comments preserved).

Per-article AI chat: `POST /api/articles/{id}/chat` streams Server-Sent Events (`data: {"delta": ...}` … `data: {"done": true}`), using `LLMAnalyzer.chat_with_article` (context = title + abstract ≤3000 chars + existing analysis ≤6000 chars + last 12 history messages). Both user question and assistant answer are persisted to `chat_messages`. Client consumes the stream with `fetch` + `response.body.getReader()` so the `X-API-Token` header still applies.

### Constants

Shared constants (fulltext char limits, etc.) live in `fetchers/models.py` and are imported by both `fetcher.py` and `analyzer.py`. Do not redefine them locally.
