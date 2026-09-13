# Acceptance mapping — spec §23

Every acceptance criterion from `refs/spec1.md` §23, mapped to its implementation
and its evidence (test names refer to `tests/`). Real-LLM behavior is verified by
the manual E2E procedure at the end — it is the one item pending an API key.

## Authentication

| Criterion | Implementation | Evidence |
|---|---|---|
| Owner can create a visitor credential | `POST /api/owner/visitors`, `POST /api/owner/visitors/{id}/credentials` (app/main.py) | `test_owner_full_flow`, `test_rate_limit_login_per_ip` fixtures; admin.html "Add visitor" dialog |
| Visitor can log in with that credential | `POST /api/visitor/login` | `test_visitor_login_and_wrong_token` |
| Credential persistently maps to exactly one visitor/workspace | `credentials.token_hash UNIQUE` (app/db.py) → `authenticate_visitor` (app/auth.py) | `test_visitor_login_and_wrong_token` + restart test |
| Owner can revoke/delete the credential | `POST /api/owner/credentials/{id}/revoke` | `test_owner_full_flow` |
| Revoked credentials stop working | `authenticate_visitor` filters `revoked_at IS NULL` | `test_owner_full_flow` (revoke → login 401) |

## Workspace

| Criterion | Implementation | Evidence |
|---|---|---|
| Each visitor has an isolated workspace | `workspaces/{visitor_id}/` (app/workspaces.py) | path-safety tests below |
| Workspace data persists on disk | Markdown files under `workspaces_dir` | `test_state_survives_restart` |
| Visitors cannot access other visitors' workspace data | Visitor routes never accept a workspace id; identity from token only (app/main.py) | `test_chat_on_foreign_session_key_fails_closed`, `test_rag_isolation_between_visitors` |
| Visitors cannot manipulate a workspace ID to bypass isolation | `visitor_dir()` validates `[0-9a-f]{32}` + `_resolve_within` containment | `test_workspace_file_editing_and_path_escape` |

## Conversations

| Criterion | Implementation | Evidence |
|---|---|---|
| Visitor can start a new session | `POST /api/visitor/sessions` | `test_create_session_and_chat_flow` |
| Visitor can continue an existing session | Chat takes `session_key`; DB ownership check | `test_create_session_and_chat_flow` |
| Conversations survive application restart | nanobot owns transcripts (JSONL); app-owned rows in SQLite; adapter `delete_all_sessions` only on owner-delete | `test_state_survives_restart` |
| The Agent receives the appropriate conversation context | nanobot session per `visitor:{id}:{key}`; runtime context provider injects profile/disclosure per turn (app/nanobot_adapter.py) | manual E2E (pending API key); unit-verified context provider in adapter |

## Agent

| Criterion | Implementation | Evidence |
|---|---|---|
| nanobot integrated through its actual Python API | `Nanobot.from_config` + `bot._loop.tools` / sessions API (app/nanobot_adapter.py) | verified against installed 0.3.0; smoke-tested at build time |
| Agent behaves as a secretary, not the owner | app/agent_home/SOUL.md persona + AGENTS.md card rules | manual E2E (pending API key) |
| The Agent can use `rag_search()` | `RagSearchTool` (app/agent_tools.py) | `test_rag_search_tool_scoped_and_graceful`, `test_rag_isolation_between_visitors` |
| The Agent can use `submit_card()` | `SubmitCardTool` (app/agent_tools) | `test_card_created_via_tool_direct`, `test_owner_card_endpoints` |

## RAG

| Criterion | Implementation | Evidence |
|---|---|---|
| `rag_search()` searches only the current visitor's workspace | RagService keyed by `visitor_id` from namespaced session key; per-workspace MicroRAG | `test_rag_isolation_between_visitors` |
| Workspace Markdown can be indexed/read by MicroRAG | `RagIndex._build_locked` (`add_documents` + `build_index`) | `test_rag_search_tool_scoped_and_graceful` |
| RAG failures are handled gracefully | never raises into agent; MicroRAG failure → workspace-local lexical fallback (app/rag.py) | `test_rag_search_tool_scoped_and_graceful` (no-binding refusal); fallback verified at build time |
| (fallback isolation) | fallback reads only `self.workspace_dir` | fallback path of `test_rag_isolation_between_visitors` |

## Cards

| Criterion | Implementation | Evidence |
|---|---|---|
| Agent can create cards without owner approval | `SubmitCardTool.execute` writes directly | `test_card_created_via_tool_direct` |
| Cards are persisted | `cards` table (app/db.py), SQLite WAL | restart test covers `cards` |
| Cards contain summary, timestamp, visitor + session association | schema: `visitor_id`, `session_key`, `summary`, `context`, `status`, `created_at` | `test_card_created_via_tool_direct` |
| Owner can view cards from dashboard | `GET /api/owner/cards` + admin.html cards view | `test_owner_card_endpoints` |

## Dashboard

| Criterion | Implementation | Evidence |
|---|---|---|
| `/admin` owner authentication works | `verify_owner_token` digest compare | `test_owner_auth_required`, `test_owner_token_high_bytes_gets_401_not_500` |
| Visitor list can be viewed | `GET /api/owner/visitors` + admin.html | `test_owner_full_flow` |
| Credentials can be created/revoked | credential routes + admin.html | `test_owner_full_flow` |
| Sessions and conversation history can be inspected | `GET /api/owner/visitors/{id}/sessions/{key}/messages` + admin.html | `test_owner_full_flow`; owner-read covered by `test_owner_full_flow` sessions payload |
| Submitted cards can be viewed | cards view in admin.html | `test_owner_card_endpoints` |

## Rate limiting

| Criterion | Implementation | Evidence |
|---|---|---|
| Visitor-facing expensive endpoints are rate limited | TokenBucketLimiter: login, sessions, messages, chat (per-IP + per-visitor) | `test_rate_limit_login_per_ip`, `test_rate_limiter_unit`, `test_daily_chat_quota` |
| Rate limiting does not affect normal owner dashboard use | no limiter checks on owner routes | no 429 assertions on owner flows pass in suite |
| (fail-closed + bounded memory) | unknown rule → deny; bucket eviction cap | `test_rate_limiter_fail_closed_on_unknown_rule`, `test_rate_limiter_eviction` |

## Deployment

| Criterion | Implementation | Evidence |
|---|---|---|
| Application runs under Uvicorn | `run.py` uvicorn entrypoint | smoke-tested; deployment/silentary.service ExecStart |
| systemd can keep it running | `Restart=always` unit with hardening | deployment/silentary.service |
| Nginx can proxy the application | bundled vhost with XFF overwrite + limits | deployment/nginx-silentary.conf |
| Survives restarts without losing state | SQLite WAL + nanobot JSONL on disk | `test_state_survives_restart` |

## Request hardening (added post-review)

| Item | Implementation | Evidence |
|---|---|---|
| M5 body-size cap | `_read_json_body` (app/main.py): 600KB cap, Content-Length pre-check + streamed backstop, 413 | `test_oversized_body_rejected_413` |
| L6 malformed JSON | `_read_json_body` returns clean 400 (also for non-object JSON) | `test_malformed_json_gets_clean_400`, `test_owner_routes_also_capped` |

## Manual E2E (pending provider API key)

The only criterion not machine-verifiable without a real LLM: a live conversation
through the real nanobot runtime. Procedure:

1. Set `SILENTARY_PROVIDER_API_KEY` (+ optional `SILENTARY_MODEL`), run `python run.py`.
2. Open `/admin`, create a visitor, copy the one-time token.
3. Log in at `/`, start a session, send messages that should trigger each tool:
   - a question answerable from workspace Markdown → verifies `rag_search`
   - a request the secretary must defer to the owner → verifies `submit_card`
4. Check `/admin` → cards view shows the card; conversation history matches.
5. Restart the process, log in again, confirm history and cards persist.

## Known deviations from spec text (documented decisions)

- MicroRAG 0.2.2's real API is `add_documents/build_index/search` (the spec's
  `load_data/ask` doesn't exist); installed with `--ignore-requires-python` on 3.11.
- nanobot 0.3.0 (installed) lacks `attributes` kwarg → visitor binding via the
  server-controlled `visitor:{visitor_id}:{session_key}` namespace.
- `similarity_threshold=0.0` because hybrid-RRF scores are small; top-k does the work.
