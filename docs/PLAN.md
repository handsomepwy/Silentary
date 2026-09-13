# Silentary — Implementation Plan (MVP)

> Source of truth: `refs/spec1.md`. This doc records environment findings and the engineering
> decisions made where the spec left details open.

## 0. Environment findings (verified)

| Fact | Consequence |
|---|---|
| Local Python 3.10 in PATH; 3.11 at `D:\Python311` (embedded dist, no `venv`/`ensurepip`) | venv created with `python -m virtualenv -p D:/Python311/python.exe .venv` (virtualenv works without ensurepip). All run commands use `.venv/Scripts/python.exe`. |
| nanobot-ai 0.3.0 requires Python ≥ 3.11 | venv uses 3.11.9 |
| MicroRAG 0.2.2 on PyPI metadata pins `>=3.12`, but the wheel compiles & runs on 3.11 | installed with `pip install --ignore-requires-python --no-deps microrag` + 3.11-compatible deps pinned separately (`numpy<2.3`, `duckdb`, `pyarrow`, `rank-bm25`, `fastembed`, `onnxruntime`). Verified working at runtime (cosine 0.67 related vs 0.09 unrelated). |
| nanobot sessions: JSONL files under `<config_dir>/sessions/`, b64-encoded session keys, atomic writes + filelocks | survive restarts out of the box; we let nanobot own session *content* |
| nanobot `bot.run(message, session_key=...)` | arbitrary external session keys accepted (`f"visitor:{visitor_id}:{session_key}"`) |
| `RequestContext.session_key` is available inside tool `execute()` | per-request visitor binding for `submit_card`/`rag_search` is recovered from the server-generated namespaced session key, never from client-controlled message text |
| `bot.runtime.add_context_provider(async fn(RequestContext) -> RuntimeContextBlock)` | injects per-turn, metadata-only context (profile/disclosure) into the prompt; runtime-context markers are stripped on history replay (`RUNTIME_CONTEXT_HISTORY_META`) so context is re-resolved fresh each turn |
| built-in tools gated by `tools.<name>.enable` config | disable exec/web/file/cli_apps/image_gen/my for visitor-facing agent; only our two tools remain |
| API keys must be in nanobot config (`${ENV}` interpolation supported) | we generate nanobot config from Silentary config at startup |

## 1. Architecture

```
Nginx (80/443) ──> uvicorn :8000  FastAPI app
                     ├── routes/visitor.py   (token auth + rate limit)
                     ├── routes/owner.py     (owner token auth)
                     ├── services/
                     │     ├── auth.py        visitor credential → visitor → workspace (fail-closed)
                     │     ├── nanobot_adapter.py  single shared Nanobot instance
                     │     ├── rag_adapter.py      per-workspace MicroRAG instances (lazy, LRU)
                     │     └── agent_tools.py      submit_card / rag_search Tool subclasses
                     ├── db.py                SQLite via sqlite3, WAL, run in threadpool
                     └── workspaces/{visitor_id}/*.md on disk
```

- **One shared `Nanobot` instance** for all visitors; isolation via namespaced session
  keys. Spec allows a separate process only if necessary — it is not: the SDK is an
  ordinary asyncio object, sessions are keyed arbitrarily, and per-session locks serialize
  same-session turns while different sessions run concurrently.
- **Visitor UI: plain HTML+JS (no framework). Owner dashboard: plain HTML+JS.** Served as
  static files by FastAPI. (Spec: prioritize functionality/reliability; no framework for
  architecture's sake.)
- **SQLite (WAL mode)** via `sqlite3` in threadpool (FastAPI sync def routes / asyncio.to_thread).
  aiosqlite avoided for simplicity (stdlib-only approach).

## 2. Security model (fail-closed)

1. **Token → visitor mapping is server-side only.** Client never sends a workspace id or
   visitor id on visitor routes. Visitor sends only the raw token + session_key.
2. **Credential hashing:** visitor tokens are random secrets (e.g. `alice-<8 urlsafe
   chars>`); DB stores only `sha256(token)` + prefix for lookup. Plaintext shown once at
   creation, never logged.
3. **session_key ownership check:** every session request verifies
   `sessions.visitor_id == auth.visitor_id` (DB), then maps to nanobot session key
   `visitor:{visitor_id}:{session_key}` — so even a guessed/foreign session_key cannot
   reach another visitor's nanobot session (key namespace is per-visitor).
4. **Agent tools:** `submit_card`/`rag_search` parse `RequestContext.session_key`
   in the server-generated form `visitor:{visitor_id}:{session_key}`, NOT from the
   message text. No valid namespaced session key → tool refuses.
5. **RAG:** per-workspace `MicroRAG` instances keyed by visitor_id, built only from that
   workspace's Markdown files. Tool resolves the workspace from the namespaced session key; no query
   parameter can cross workspaces.
6. **Path safety:** workspace dir for a visitor is `workspaces/{visitor_id}` where
   visitor_id is DB-issued (UUID hex). Files listed by `os.walk`, resolved and
   `relative_to(root)`-validated; `.md` only for agent-visible reading; workspace root
   validated to be inside the global workspaces root.
7. **Owner token:** separate constant-time-compared token from env/config file; all
   `/api/owner/*` guarded. No rate limit on owner routes (spec).
8. **Rate limiting:** in-process token-bucket per (key, bucket) — login attempts (per-IP+
   per-token), chat sends, RAG-index rebuilds. 429 with clear JSON. No Redis.
9. **No stack traces/paths to visitors:** generic error handler; 500s sanitized; debug
   details only in server logs.
10. **LLM output is content, not instructions:** agent output is rendered as escaped
    markdown; no innerHTML injection of model text.

## 3. Data model (SQLite)

- `visitors(id TEXT PK, name TEXT NOT NULL, relationship TEXT DEFAULT '', disclosure_boundary TEXT DEFAULT '', created_at REAL, updated_at REAL)`
  - `disclosure_boundary` = free Markdown text (agent-facing rule text), may be empty.
- `credentials(id TEXT PK, visitor_id TEXT NOT NULL REFERENCES visitors(id), token_hash TEXT NOT NULL UNIQUE, token_prefix TEXT NOT NULL, created_at REAL, revoked_at REAL NULL)`
  - Active credential = `revoked_at IS NULL`. Login: hash lookup → visitor.
- `sessions(visitor_id TEXT, session_key TEXT, created_at REAL, updated_at REAL, title TEXT DEFAULT '', PRIMARY KEY(visitor_id, session_key))`
  - Metadata only; content lives in nanobot's JSONL store.
- `cards(id TEXT PK, visitor_id TEXT NOT NULL, session_key TEXT NOT NULL, created_at REAL, summary TEXT NOT NULL, context TEXT NULL, status TEXT NOT NULL DEFAULT 'unread')`
- Schema migrations: `PRAGMA user_version` counter, applied at startup.

## 4. nanobot integration

- Startup: build `config.json` for nanobot from Silentary settings (provider/model/api
  key from env), then `Nanobot.from_config(config_path, workspace=<shared workspace>)`.
- **Shared agent workspace** (`data/nanobot/agent/`) holds SOUL.md (secretary persona +
  card rules), AGENTS.md (operating rules). Per-visitor info is NOT placed here.
- Register after construction: `bot._loop.tools.register(SubmitCardTool(...))`,
  `bot._loop.tools.register(RagSearchTool(...))`.
- Context provider: resolves visitor profile/disclosure blocks per turn from
  the `RequestContext.session_key` prefix `visitor:{id}:`, reads the visitor
  workspace's `profile.md` + disclosure text, bounded
  (e.g. 4k chars), wrapped in RuntimeContextBlock(source="silentary_workspace").
- Session keys: `visitor:{visitor_id}:{session_key}`; channel label `silentary`.
- `submit_card(summary, context=None)`: writes a row to `cards` + updates
  `sessions.updated_at`; returns confirmation text to the LLM.
- `rag_search(query)`: resolves workspace from the namespaced session key; lazily builds/reuses a
  per-workspace MicroRAG index (invalidated by workspace file mtimes); returns top-k
  snippets (top_k=6, threshold 0.0). If MicroRAG or its numeric dependencies are
  unavailable, it falls back to a small same-workspace Markdown keyword search.
  Any retrieval error degrades gracefully and never raises into the agent loop.
- On shutdown: `await bot.aclose()`.

## 5. API surface

Visitor (auth: `Authorization: Bearer <token>`, re-validated on every request):
- `POST /api/visitor/login` {token} → validates, returns visitor display name. Exists so
  brute-force attempts are rate-limited in one place; subsequent calls re-auth each time
  (stateless, no cookies/sessions server-side).
- `GET  /api/visitor/sessions` → list own sessions (metadata only)
- `POST /api/visitor/sessions` → create session (server generates session_key)
- `GET  /api/visitor/sessions/{session_key}/messages` → history (from nanobot session)
- `POST /api/visitor/chat` {session_key, message} → {reply} (full agent turn)
- `DELETE /api/visitor/sessions/{session_key}` → delete own session

Owner (auth: `Authorization: Bearer <owner_token>` from env/config):
- `GET    /api/owner/visitors` → list
- `POST   /api/owner/visitors` {name, relationship, disclosure_boundary} → creates
  visitor + workspace dir + first credential (returns plaintext token exactly once)
- `POST   /api/owner/visitors/{id}/credentials` → issue another credential (token returned once)
- `POST   /api/owner/credentials/{cred_id}/revoke` → revoke
- `GET    /api/owner/visitors/{id}` → detail: credentials (hashes/prefixes only), sessions, cards
- `GET    /api/owner/visitors/{id}/sessions/{session_key}/messages` → read chat history
- `GET    /api/owner/cards` → all cards (filter by unread)
- `POST   /api/owner/cards/{id}/status` {status} → mark read/resolved
- `GET    /api/owner/visitors/{id}/workspace` → list workspace markdown files
- `PUT    /api/owner/visitors/{id}/workspace/{filename}` → edit a markdown file (re-indexes RAG lazily)

## 6. Cards

- Created by the agent tool only. Owner cannot create cards; owner can mark read/resolved.
- Fields per spec: id, visitor_id, session_key, created_at, summary, context, status.

##  cards persisted in SQLite; dashboard lists unread first

## 7. Rate limiting (token bucket, in-process)

| Bucket | Capacity | Refill |
|---|---|---|
| login per-IP | 10/min | 1/6s |
| login per-token | 5/min | 1/12s |
| chat per-token | 20/min | 1/3s |
| chat per-IP | 30/min | 1/2s both must pass |
| rag rebuild | 10/min per visitor |  implicit (index rebuilds are automatic + debounced by mtime) |

- Persisted? No — in-memory is fine for MVP (reset on restart only loosens limits briefly;
  DB-backed buckets would add coupling for little gain). Spec allows in-process for
  single-server MVP.

## 8. Testing

- pytest + httpx AsyncClient (ASGI transport, no real LLM calls).
- nanobot adapter is injected/faked in most tests via a stub `AgentService` protocol.
- Real MicroRAG used in rag adapter tests (fastembed model download once per test session; skipped if offline).
- Isolation tests: cross-visitor token use, session key guessing, workspace path escape,
  tool attribute requirement, rate limit 429s, revoked credentials.

## 9. Out of scope / deferred (recorded per spec §24)

- WeChat import pipeline, OAuth/JWT, Redis, multi-node, frontend framework, analytics.
