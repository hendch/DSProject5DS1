# Guardrail Agent

Every action another agent proposes passes through the guardrail. It decides
**auto-approve**, **hold for a human**, or **reject**, records why, and never
executes anything itself.

```
pipeline_events ──► validate ──► real risk ──► OPA policy ──► LLM may escalate
                                                                   │
        approval_request / approval_result ◄── save + hashed audit row (Postgres)
                                                                   │
                         human ──► FastAPI /approve, /reject ──────┘
```

## The four layers

| Layer | File | What it does |
|---|---|---|
| 1. Policy | `policy/guardrail.rego` (OPA) | Environment × risk rules. High risk always needs a human; prod + high needs **two** people; agents may only emit their own action types; anything unmatched holds (fail closed). |
| 2. Real risk | `risk.py` | Ignores the agent's self-label as a ceiling: scans the payload for prod, destructive operations, IAM/secrets/network, billable resources, state; bumps one level when the agent used a fallback (`heuristic`/`static`). Can only **raise** the risk. Advice (`rank_causes`) is not scanned. |
| 3. LLM judge | `llm_judge.py`, `prompts/judge.txt` | Writes a plain-English summary for the reviewer and can turn an auto-approve into a hold. It can **never** approve. Proposal text is fenced as untrusted. Ollama down → skipped, policy decision stands. |
| 4. Humans | `api.py`, `store.py` | Atomic votes (`SELECT … FOR UPDATE`), no self-approval, one vote per person, two-person rule, approvals expire (default 4 h). |

Plus a **tamper-evident audit log**: a Postgres trigger chains every
`reasoning_logs` row (from every agent) with SHA-256; UPDATE, DELETE and
TRUNCATE are refused; `verify_reasoning_chain()` reports any row edited or
removed behind the trigger's back.

### Policy matrix (for a change; advice is auto-approved unless labelled high)

|         | low  | medium | high |
|---------|------|--------|------|
| dev     | auto | auto   | human |
| staging | auto | human  | human |
| prod    | human| human  | 2 humans |

## Try it: the playground (no Docker needed)

```powershell
python -m pip install -r requirements-dev.txt
python -m agents.guardrail.playground        # opens http://localhost:8090
```

One command starts a throwaway Postgres, an in-memory Redis, OPA with the real
policy, the guardrail loop and the API, plus a page where you can:

- send ready-made proposals (the ShopLine scenarios: Friday IAM cycle, prod
  change needing two people, "trivial" merge touching prod, malformed event…),
  each showing the outcome it should produce;
- approve / reject as sami, ines, karim or anyone, and see 403 / 409 / 410;
- open "why?" on any decision: risk signals, OPA rule, LLM review, audit trail;
- watch the stream exactly as the Orchestrator will see it;
- tamper with the audit log as a DB admin and watch the hash chain catch it;
- simulate an OPA outage (fail closed) and toggle the LLM judge;
- edit `policy/guardrail.rego` while it runs: OPA reloads it live.

OPA is taken from `OPA_BIN`, your PATH, or downloaded once into `.tools/`.
Every launch starts from an empty database. `--docker` uses the
docker-compose services from `.env` instead. This page is a dev tool for the
guardrail only; the team dashboard is the Orchestrator's job.

## Using a hosted LLM instead of Ollama

The LLM judge can call an API instead of a local model. In `.env`:

```ini
LLM_PROVIDER=openai                          # any OpenAI-compatible API
LLM_BASE_URL=https://api.groq.com/openai/v1  # Groq: free tier, no card
LLM_API_KEY=<your key>
LLM_MODEL=llama-3.1-8b-instant               # same Llama 3.1 8B the plan runs locally
```

or `LLM_PROVIDER=anthropic` with `LLM_API_KEY` and `LLM_MODEL=claude-haiku-5-5`
(paid per token). Back to local: `LLM_PROVIDER=ollama`.

Things to know: proposals (including infrastructure code) are sent to the
provider, so use it for test data, and switch back to Ollama for anything
real. Free tiers are rate-limited; a 429 makes the judge step aside for 30 s
and the policy decision stands, as with any LLM failure.

## Run it

```powershell
docker-compose up -d                     # postgres, redis, chroma, opa
python -m pip install -r requirements.txt
python -m agents.guardrail.agent         # the consumer loop (Ctrl+C to stop)
uvicorn agents.guardrail.api:app --port 8080   # approvals API, docs at /docs
```

Existing Postgres volume (created before this branch)? Apply the migration once:

```powershell
Get-Content db/migrations/002_guardrail.sql | docker compose exec -T postgres psql -U devops -d agents
```

Other commands: `python -m agents.guardrail.agent --once` (drain and exit),
`--verify` (check the audit chain). Settings are in `.env.example`.

## API

| Method | Path | Notes |
|---|---|---|
| GET | `/pending-approvals` | Pending, not expired, with risk, rule, reason, LLM summary, votes so far |
| GET | `/approvals/{id}` | One approval |
| POST | `/approve/{id}` | Body `{"decided_by": "ines"}`. 403 own action, 409 already decided/voted, 410 expired |
| POST | `/reject/{id}` | Same body. One reject is final |
| GET | `/audit/verify` | `{"intact": true, "broken_rows": []}` |

Decisions are announced on `pipeline_events` as `approval_request` (pending)
or `approval_result` (approved / rejected), with `payload.action_ref` = the
original `event_id`, so the Orchestrator can resume or stop the run.

## Tests

```powershell
python -m pip install -r requirements-dev.txt
python -m unittest discover -s agents -t . -p "test_*.py"
opa test agents/guardrail/policy -v     # or: docker run --rm -v "${PWD}/agents/guardrail/policy:/policy" openpolicyagent/opa:1.20.2 test /policy -v
```

The database tests run against a real PostgreSQL 16 (from `pgserver`, or
`TEST_DATABASE_URL`); `test_flow.py` also needs the `opa` binary (`OPA_BIN`).

## Reliability

- Redis **consumer group**: an event is acknowledged only after its decision is
  committed. Crash or DB down → it is retried; after 5 failures it is parked in
  `pipeline_events_dead`.
- Redelivered events are not decided twice (`UNIQUE(action_id)`).
- OPA unreachable → hold (fail closed). Malformed event → rejected and audited.

## Needs agreement from the team

- **Schema**: `db/migrations/002_guardrail.sql` adds columns to `approvals`, a
  new `approval_votes` table, and the hash chain + append-only triggers on
  `reasoning_logs` (covers every agent's rows).
- **docker-compose**: adds the `opa` service and mounts the migration
  (`schema.sql` is now mounted as `001_schema.sql` so it runs first).
- **Event schema**: proposals should carry who triggered them
  (`payload.triggered_by`, e.g. the GitHub actor) so self-approval can be
  blocked; without it that check is skipped.
- **`publish_and_log`** writes to Redis *before* Postgres, so the guardrail can
  see an event before its `actions` row exists. It retries, so this is safe,
  but swapping the order in `shared/events.py` would remove the delay.

## Known limits

- `decided_by` is not authenticated yet (MVP); add a login before approvals
  trigger real changes.
- Keyword risk scoring errs on the side of holding; false alarms cost a click.
- The hash chain detects edits and deletions in the middle of the log, not
  removal of the newest rows; anchoring the latest hash elsewhere would close that.
