# DSProject5DS1: multi-agent DevOps / MLOps assistant

Specialist agents analyse problems and **propose** actions. The **guardrail** decides
whether each proposal is approved automatically, held for a human, or rejected. The
**orchestrator** (in progress) routes events to the right agents and executes only what
was approved. Every step is recorded in one tamper-evident audit log.

## Layout

```
pyproject.toml          one environment for every agent:  pip install -e ".[dev]"
.env.example            one settings file for every agent (copy to .env)
docker-compose.yml      postgres, redis, chroma, opa
db/                     schema.sql + migrations/ (run in order on a fresh database)
shared/                 code every agent uses: database, Redis event bus, publish_and_log
agents/
  dependency_resolver/  Lyna            circular dependencies in OpenTofu code
  diagnostic_rca/       zrharounesprit  root causes of failed CI builds (RAG on Chroma)
  merge_assistant/      Muaad           git merge conflicts and proposed resolutions
  code_reviewer/        hendch          ML/MLOps review of pull requests, safe lint fixes
  drift_detection/      omar            data and concept drift on a deployed model
  guardrail/            Rayen           policy gate, human approvals, audit log
  orchestrator/         (to build)      routing, pause/resume, execution
corpus/                 knowledge base ingestion for the RCA agent (to build)
demo/                   code reviewer local demo scenarios
ml_demo/                small ML project targeted by the seeded demo pull request
.github/workflows/      code review agent on pull requests
```

Each agent keeps its own README and tests in its folder.

## Setup

Requires Python 3.12 and Docker Desktop.

```bash
python -m venv venv
venv\Scripts\activate              # Windows (source venv/bin/activate elsewhere)
python -m pip install -e ".[dev]"
copy .env.example .env             # then fill in keys if you use a hosted LLM
docker compose up -d
```

LLM: by default every agent uses a local model through [Ollama](https://ollama.com)
(`ollama pull llama3.1:8b`). Any OpenAI-compatible endpoint (Hugging Face router, xAI…)
works by changing `LLM_BASE_URL`, `LLM_API_KEY` and `LLM_MODEL` in `.env`.

## Running the agents

| Agent | Command |
|---|---|
| Dependency Resolver | `python -m agents.dependency_resolver.agent` |
| Diagnostic / RCA | `python -m agents.diagnostic_rca.agent [log file]` |
| Merge Assistant | `python -m agents.merge_assistant.agent` |
| Code Reviewer | `python demo/run_local_demo.py --scenario leaky_pr` |
| Drift Detection | `drift-agent init`, then `drift-agent serve` (port 8001) |
| Guardrail | `python -m agents.guardrail.playground` (everything in one command, port 8090) |

## Tests

```bash
python -m pytest
```

## Integration status

The branches of all six agents are merged here and share one environment. Known
issues before the orchestrator can link them:

- **drift_detection**: its `data/` package (dataset download and simulator) was never
  pushed, because the old `.gitignore` hid every folder named `data/`. Push it with
  `git add agents/drift_detection/data` (the rule is fixed); until then the agent can't
  start. Its generated files now go to `agents/drift_detection/runtime/`.
- **drift_detection** has its own SQLite store and approval flow, separate from the
  guardrail; both move to the shared ones in a later phase.
- **code_reviewer**: the workflow still checks out the agent from the `code-reviewer`
  branch (`vars.AGENT_REF`). Point it at this branch once merged. Its baseline
  `pytest` runs at the repository root, which now holds every agent's tests; scope it to
  `ml_demo/`.
- **diagnostic_rca** and **merge_assistant** have no LLM fallback yet.
- Only the guardrail, dependency_resolver, diagnostic_rca and merge_assistant use the
  shared event bus; code_reviewer and drift_detection get adapters in the orchestrator.
