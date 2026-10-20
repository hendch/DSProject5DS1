# OpsAgents: an AI control centre for your repositories

Connect your GitHub repositories and OpsAgents watches every commit, CI run and pull
request. When something breaks, specialist agents investigate on their own; every action
they propose goes through a guardrail; anything risky waits for a human; and everything is
recorded in a tamper-evident audit log.

```
GitHub (commits · CI · PRs) ─┐                      ┌─► Dependency Resolver   OpenTofu cycles
Dashboard (scenarios, text) ─┼─► ORCHESTRATOR ──────┼─► Diagnostic / RCA      root causes (RAG)
                             │   rules → LLM triage │─► Merge Assistant       merge conflicts
                             │   → human triage     ├─► Code Reviewer         ML/MLOps PR review
                             │                      └─► Drift Detection       model drift
                             │          proposals ▼
                             │   GUARDRAIL: real risk · OPA policy · LLM judge (escalate only)
                             │          auto-approve │ hold for humans │ reject
                             └─► execute approved actions · alerts · hash-chained audit log
```

## Start it

Requires Python 3.12. No Docker needed for a demo.

```bash
python -m venv venv
venv\Scripts\activate                 # Windows (source venv/bin/activate elsewhere)
python -m pip install -e ".[dev]"
copy .env.example .env                # LLM settings (local Ollama by default)
python -m app                         # opens http://localhost:8080
```

`python -m app` starts an embedded PostgreSQL (with pgvector) and OPA, applies every
migration, then runs the guardrail, the orchestrator, the GitHub watcher and the
dashboard. Stop it with Ctrl+C. Each start uses a fresh database; for a persistent one:

```bash
docker compose up -d                  # PostgreSQL + OPA
python -m shared.migrate
python -m app --docker
```

## Use it

- **Home**: repository health, start a demo scenario, describe an incident in plain text.
- **Activity**: every commit and pull request with its CI status (green / red / running);
  investigate any failure in one click.
- **Runs**: each investigation's agent steps, proposals, decisions and timeline.
- **Approvals**: approve or reject held proposals (no self-approval; production + high
  risk needs two people; approvals expire).
- **Triage**: incidents the rules and the LLM couldn't route; a human picks the route.
- **Alerts**: CI failures, recoveries, approvals needed, agents unavailable; desktop
  notifications if you allow them.
- **Analytics**: runs, decisions, review findings, CI pass rate, recovery time, per agent.
- **Agents / Audit log / Settings**: each agent's model and health, the hash-chain check,
  GitHub connection and options.

### Connect GitHub

Create a fine-grained token with **read** access to *Contents*, *Actions*, *Checks* and
*Pull requests* on the repositories you want to follow, then paste it in **Settings**
(or set `GITHUB_TOKEN` in `.env`). Add more repositories from **Repositories**. The app
polls GitHub every 60 s (webhooks can't reach a laptop); existing history is recorded
without alerts, and each new failure is alerted and investigated exactly once. With
*auto-investigate* on, a failed CI run starts the RCA agent on the real job log (secrets
removed) and a new pull request gets a code review.

## Models

Every agent talks to an OpenAI-compatible endpoint (local Ollama, the Hugging Face router,
xAI…), set once in `.env` with per-agent overrides:

| Role | Default | Why |
|---|---|---|
| Dependency Resolver, Merge Assistant, Drift, orchestrator triage | `llama3.1:8b` | one consistent default |
| Diagnostic / RCA | reasoning model (`DIAGNOSTIC_RCA_LLM_MODEL`) | multi-step diagnosis |
| Code Reviewer | code model (`CODE_REVIEWER_LLM_MODEL`) | reads diffs |
| Guardrail judge | different family (`GUARDRAIL_LLM_MODEL`) | independent from the agents it judges |

When a model is unreachable, every agent falls back to rules and the run is marked
*degraded*; the guardrail's rules never depend on an LLM.

## Safety by design

- Agents only **propose**; only the orchestrator executes, and only after approval.
- The guardrail recomputes risk from evidence (it never lowers an agent's own label),
  applies version-controlled OPA rules, and its LLM judge can only make decisions stricter.
- Fail closed: policy engine down → everything is held for a human.
- Text written by agents, commits or CI logs is treated as data, never as instructions.
- GitHub actions are dry runs unless `ORCHESTRATOR_GITHUB_PUBLISH=true`; nothing ever merges.
- One PostgreSQL holds everything; an action and its event are written in one transaction;
  the audit log is append-only and hash-chained.

## Layout

```
app/                    python -m app: API (FastAPI) + launcher
web/                    the dashboard (static, no build step)
agents/
  orchestrator/         routing, run lifecycle, adapters, executors, GitHub watcher, analytics
  guardrail/            Rayen           risk, OPA policy, LLM judge, approvals, audit
  dependency_resolver/  Lyna            circular dependencies in OpenTofu code
  diagnostic_rca/       zrharounesprit  root causes of failed builds (RAG on pgvector)
  merge_assistant/      Muaad           merge conflicts and proposed resolutions
  code_reviewer/        hendch          ML/MLOps review of pull requests, safe lint fixes
  drift_detection/      omar            data and concept drift on a deployed model
shared/                 database, event bus, knowledge base, LLM client, alerts, migrations
db/migrations/          numbered SQL files (python -m shared.migrate)
demo/                   demo scenarios (code review repos, IAM cycle)
ml_demo/                small ML project targeted by the seeded demo pull request
tests/                  platform tests; each agent keeps its own tests in its folder
```

## Tests

```bash
python -m pytest
```

They run on a throwaway PostgreSQL and the real OPA policy; no Docker or LLM needed.

## Known gaps

- **drift_detection**: its `data/` package (dataset download and simulator) was never
  pushed; until it is, the agent shows as *unavailable*. Push it with
  `git add agents/drift_detection/data`.
- **code_reviewer**: its own GitHub workflow still checks out the agent from the
  `code-reviewer` branch (`vars.AGENT_REF`) and runs `pytest` at the repository root;
  scope it to `ml_demo/` once merged.
- Approvers type their name: real sign-in is needed before approvals change real systems.
- The knowledge base holds 5 sample documents until `corpus/ingest.py` (Member E) fills it.
