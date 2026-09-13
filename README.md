# Silentary — Personal AI Secretary Platform

Self-hosted platform where visitors chat with an AI secretary that represents
you (the owner) when you're offline. Built on [nanobot](https://github.com/HKUDS/nanobot)
(agent runtime) and MicroRAG (per-visitor workspace retrieval).

## How it works

```
Visitor ──token──> FastAPI ──> nanobot Agent (secretary persona)
                    │               ├─ rag_search()   → visitor's private workspace index
                    │               └─ submit_card()  → attention card for you
                    ├─ SQLite (visitors, credentials, sessions, cards)
                    └─ workspaces/{visitor_id}/*.md (your prepared Markdown)
Owner ──owner token──> /admin dashboard (visitors, credentials, history, cards)
```

- **One credential ↔ one visitor ↔ one workspace.** The token is resolved
  server-side; visitors never send a workspace or visitor id (fail-closed).
- **Sessions are persistent** (nanobot JSONL session store + SQLite metadata);
  conversations survive restarts.
- **RAG is per-workspace**: a MicroRAG index is built per visitor from their
  workspace Markdown, rebuilt automatically when files change.
- **Cards** let the agent escalate important matters to you without approval.

## Quick start (local dev)

Requirements: Python **3.11+**.

```bash
# 1. create venv
python -m venv .venv                 # or: python -m virtualenv -p <py311> .venv
.venv/Scripts/pip install -r requirements.txt
# microrag's metadata pins >=3.12 but runs fine on 3.11:
.venv/Scripts/pip install --ignore-requires-python --no-deps microrag

# 2. configure — set provider key (SILENTARY_OWNER_TOKEN optional in dev;
#    if unset, a token is generated to data/owner_token.txt)
set SILENTARY_PROVIDER_API_KEY=sk-ant-...
set SILENTARY_MODEL=anthropic/claude-opus-4-5

# 3. run
.venv/Scripts/python run.py          # serves http://127.0.0.1:8000
```

- Visitor site: `http://127.0.0.1:8000/`
- Owner dashboard: `http://127.0.0.1:8000/admin`

## Configuration

All settings come from environment variables or a JSON config file
(`SILENTARY_CONFIG`, default `data/config.json`). See
`deployment/.env.example`.

| Variable | Purpose |
|---|---|
| `SILENTARY_OWNER_TOKEN` | Owner dashboard token (required in production) |
| `SILENTARY_PROVIDER_API_KEY` | LLM API key (required for the agent) |
| `SILENTARY_MODEL` | Model in `provider/model` format |
| `SILENTARY_PROVIDER` | Override provider name (defaults to model prefix) |
| `SILENTARY_HOST` / `SILENTARY_PORT` | Bind address (default 127.0.0.1:8000) |
| `SILENTARY_DATA_DIR` | SQLite db + nanobot sessions (default `data/`) |
| `SILENTARY_WORKSPACES_DIR` | Per-visitor Markdown (default `workspaces/`) |

## Owner workflow

1. Open `/admin`, sign in with the owner token.
2. **Add visitor** → set name/relationship/disclosure boundary → save the
   one-time token and hand it to the visitor.
3. Put knowledge the agent may use for that visitor in their workspace files
   (`profile.md`, `history.md`, …) via the dashboard editor.
4. Review **Cards** for matters needing your attention; mark read/resolved.
5. Revoke credentials any time; revoked tokens stop working immediately.

## Deployment (Linux server + systemd + Nginx)

```bash
# 1. app
sudo useradd -r -s /usr/sbin/nologin silentary
sudo mkdir -p /opt/silentary && sudo chown silentary: /opt/silentary
# copy repo into /opt/silentary, then as silentary user:
cd /opt/silentary && python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install --ignore-requires-python --no-deps microrag

# 2. secrets
cp deployment/.env.example /opt/silentary/.env   # fill in tokens/keys
chmod 600 /opt/silentary/.env

# 3. service
sudo cp deployment/silentary.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now silentary

# 4. nginx
sudo cp deployment/nginx-silentary.conf /etc/nginx/sites-available/silentary
sudo ln -s /etc/nginx/sites-available/silentary /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d silentary.example.com     # HTTPS
```

The app binds to 127.0.0.1:8000 only; Nginx terminates TLS on 80/443 and
proxies. The dashboard lives behind `/admin` on the same vhost (protected by
the owner token; no extra port is exposed).

### Backups

Everything stateful lives in two directories:

```bash
tar czf silentary-backup.tar.gz /opt/silentary/data /opt/silentary/workspaces
```

SQLite is in WAL mode; for a hot backup use `sqlite3 data/silentary.db ".backup ..."`
or stop the service briefly.

## Tests

```bash
.venv/Scripts/python -m pytest tests/ -q
```

23 tests cover auth (visitor + owner), session ownership, workspace path-escape,
RAG cross-visitor isolation, card persistence, rate limiting, and the
agent-unavailable degradation path.

## Architecture notes (see docs/PLAN.md for details)

- **nanobot integration** goes through `app/nanobot_adapter.py` only. Sessions
  are namespaced `visitor:{visitor_id}:{session_key}` so a session can never
  escape its visitor. Per-turn visitor context (profile/disclosure) is injected
  via nanobot's runtime-context provider; custom tools (`submit_card`,
  `rag_search`) resolve the visitor from the server-controlled session key.
- **nanobot built-in tools** (shell/file/web/etc.) are disabled via config; the
  agent can only use the two Silentary tools.
- **Rate limiting** is an in-process token bucket (login/chat/messages); Nginx
  adds coarse IP limits on top.
- **Microrag** is installed with `--ignore-requires-python` on 3.11 (metadata
  pins ≥3.12; wheel verified working). When your deploy target has 3.12+, drop
  the flag. If MicroRAG or its numeric dependencies are unavailable, Silentary
  falls back to a small same-workspace Markdown keyword search so retrieval
  degrades safely without crossing visitor boundaries.

## Known limitations (MVP)

- No streaming responses (one `POST /chat` per message; turn takes seconds).
- Rate-limit buckets reset on restart (acceptable single-server MVP).
- One shared LLM provider/model for all visitors.
- RAG index is per-process; many concurrent visitors rebuild lazily (bounded
  LRU of 16 indexes).
- Dashboard/session transcript reads go through nanobot's session store —
  requires the agent service to be up (returns 503 otherwise).
