# OpsAgents: an AI control centre for your repositories

OpsAgents is a multi-agent system for DevOps and MLOps incidents. Connect your GitHub
repositories and it watches every commit, CI run and pull request. When something breaks,
specialist agents investigate on their own. Every action they propose goes through a
guardrail, anything risky waits for a human, and everything is recorded in a tamper-evident
audit log.

**Agents propose. The guardrail decides. Humans approve what matters.**

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

Contents:
[Quick start](#quick-start) ·
[Requirements](#requirements) ·
[Install and run, step by step](#install-and-run-step-by-step) ·
[Deploy to Render](#deploy-to-render) ·
[Configuration](#configuration) ·
[Using the dashboard](#using-the-dashboard) ·
[Connect GitHub](#connect-github) ·
[How it works](#how-it-works) ·
[The agents](#the-agents) ·
[The guardrail](#the-guardrail) ·
[Data](#data) ·
[Tests](#tests) ·
[API](#api) ·
[Project layout](#project-layout) ·
[Troubleshooting](#troubleshooting) ·
[Known gaps](#known-gaps) ·
[Team](#team)

---

## Quick start

```bash
git clone https://github.com/hendch/DSProject5DS1.git
cd DSProject5DS1
git switch orchestrator
python -m venv venv
venv\Scripts\activate                     # Windows; on macOS/Linux: source venv/bin/activate
python -m pip install -r requirements.txt
copy .env.example .env                    # macOS/Linux: cp .env.example .env, then add your LLM key
python -m app                             # opens http://localhost:8080
```

On the Home page, start **Failed deploy: IAM cycle** to see a complete run in about 20 seconds.

---

## Requirements

| What | Version | Notes |
|---|---|---|
| Python | 3.12 or newer | Tested on 3.12.10 (Windows 11). |
| Python packages | see [`requirements.txt`](requirements.txt) | Every package pinned, including the ones installed with them. |
| PostgreSQL 16 + pgvector | embedded | Started for you by `pgserver`. Docker is only needed for a persistent database. |
| OPA (Open Policy Agent) | 1.20.2 | Downloaded into `.tools/` on the first start if it isn't on your `PATH`. |
| An LLM endpoint | any OpenAI-compatible API | Hugging Face router, xAI, or a local Ollama. Without one, every agent falls back to rules. |
| OpenTofu | optional | Lets the Dependency Resolver run `tofu graph`. Without it, it parses the `.tf` files itself. |
| Git | any recent version | Used by the Merge Assistant and the Code Reviewer. |
| Disk / RAM | about 2 GB / 4 GB | Mostly the embedding model (`BAAI/bge-small-en-v1.5`), downloaded on first use. |

There are two ways to install the packages, and both give the same set:

```bash
python -m pip install -r requirements.txt   # exact pinned versions (recommended)
python -m pip install -e ".[dev]"           # from pyproject.toml, looser version ranges
```

`pip install -e .` also adds the `drift-agent` command. After installing from
`requirements.txt`, run `python -m pip install -e . --no-deps` if you want it.

---

## Install and run, step by step

### 1. Get the code

```bash
git clone https://github.com/hendch/DSProject5DS1.git
cd DSProject5DS1
git switch orchestrator          # the integration branch with every agent wired together
```

### 2. Create a virtual environment

```bash
python -m venv venv
```

Activate it. Do this in every new terminal:

| System | Command |
|---|---|
| Windows (PowerShell) | `venv\Scripts\Activate.ps1` |
| Windows (cmd) | `venv\Scripts\activate.bat` |
| macOS / Linux | `source venv/bin/activate` |

If PowerShell refuses to run the script, run
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once.

### 3. Install the packages

```bash
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

### 4. Create your settings file

```bash
copy .env.example .env           # Windows
cp .env.example .env             # macOS / Linux
```

Open `.env` and choose where the models run. See [Configuration](#configuration) for the
full list. These are the three common set-ups:

**Hugging Face router** (what the team uses; a free account works):

```ini
LLM_BASE_URL=https://router.huggingface.co/v1
LLM_API_KEY=hf_your_token
LLM_MODEL=meta-llama/Llama-3.1-8B-Instruct
DIAGNOSTIC_RCA_LLM_MODEL=deepseek-ai/DeepSeek-R1-Distill-Qwen-7B
DIAGNOSTIC_RCA_LLM_JSON_MODE=false
DIAGNOSTIC_RCA_LLM_MAX_TOKENS=2500
GUARDRAIL_LLM_MODEL=Qwen/Qwen2.5-7B-Instruct:featherless-ai
```

**xAI** for the Code Reviewer and Drift Detection (they need reliable tool calls):

```ini
CODE_REVIEWER_LLM_BASE_URL=https://api.x.ai/v1
CODE_REVIEWER_LLM_API_KEY=xai_your_key
CODE_REVIEWER_LLM_MODEL=grok-4.20-0309-non-reasoning
XAI_API_KEY=xai_your_key
XAI_MODEL=grok-4.20-0309-reasoning
```

**Local Ollama** (offline; the defaults in `.env.example`):

```bash
ollama pull llama3.1:8b
```

```ini
LLM_BASE_URL=http://localhost:11434/v1
LLM_API_KEY=ollama
LLM_MODEL=llama3.1:8b
```

Never commit `.env`. It is in `.gitignore`.

### 5. Start the platform

```bash
python -m app
```

The first start takes a minute or two. It downloads OPA and the embedding model. After that,
it takes a few seconds. In order, it:

1. starts an embedded PostgreSQL 16 with pgvector and an OPA server on free local ports;
2. applies every migration in `db/migrations/`;
3. seeds the knowledge base with the sample CI/CD documents (skip with `--no-seed`);
4. starts the guardrail, the orchestrator and its event consumer, and the GitHub watcher;
5. serves the dashboard and the API on http://localhost:8080 and opens your browser.

Stop it with **Ctrl+C**. The embedded database is thrown away on every start.

Options:

| Option | Default | What it does |
|---|---|---|
| `--port 8080` | `APP_PORT` or 8080 | Port for the dashboard and the API. |
| `--host 127.0.0.1` | `APP_HOST` or 127.0.0.1 | Use `0.0.0.0` to reach it from other machines (the app has no login, so be careful). |
| `--no-browser` | off | Don't open a browser tab. |
| `--no-seed` | off | Don't add the sample documents to the knowledge base. |
| `--docker` | off | Use the PostgreSQL and OPA in `.env` instead of the embedded ones. |
| `--cloud` | off | Use the PostgreSQL in `DATABASE_URL` and start OPA next to the app (hosting, see [Deploy to Render](#deploy-to-render)). |

### 6. (Optional) A persistent database with Docker

```bash
docker compose up -d             # PostgreSQL 16 + pgvector on 5432, OPA on 8181
python -m shared.migrate         # create or update the tables
python -m app --docker
```

`docker-compose.yml` mounts `agents/guardrail/policy/` into OPA, so policy edits are
picked up live.

### 7. (Optional) Install OpenTofu

With [OpenTofu](https://opentofu.org/docs/intro/install/) on your `PATH`, the Dependency
Resolver reads the real `tofu graph`. Without it, the run is marked *degraded* and the
resolver falls back to its static `.tf` parser, which still finds the demo cycle.

### 8. (Optional) Share the dashboard

The app listens on `127.0.0.1` only. To show it to someone else, open a tunnel to
`127.0.0.1:8080`. Use the IP, not `localhost`, which can give 502 errors over IPv6:

```bash
ssh -o ServerAliveInterval=30 -R 80:127.0.0.1:8080 nokey@localhost.run
```

Set `APP_PASSWORD` first (see [Deploy to Render](#deploy-to-render)). Without it, anyone
with the link can approve actions.

---

## Deploy to Render

The repository is ready for [Render](https://render.com). It contains:

| File | Purpose |
|---|---|
| [`Dockerfile`](Dockerfile) | Python 3.12 image with git, OPA 1.20.2 and OpenTofu 1.9.1. The embedding model is downloaded at build time. The app runs as a non-root user. |
| [`render.yaml`](render.yaml) | Blueprint: the web service plus a PostgreSQL 16 database, with every setting wired up. |
| [`.dockerignore`](.dockerignore) | Keeps `.env`, `venv/`, `.git` and generated files out of the image. |

In the image, the app starts with `python -m app --cloud`. That means:

- the database is the one in `DATABASE_URL` (Render Postgres), so data survives restarts and
  deploys, and migrations run on every start;
- OPA runs inside the container, next to the app;
- the app listens on `0.0.0.0` on Render's `PORT`;
- with `APP_PASSWORD` set, every page and API call needs a sign-in (HTTP Basic, user
  `APP_USERNAME`). `/api/health` stays open for Render's health check.

### Steps

1. Push the branch you want to deploy to GitHub.
2. On Render: **New → Blueprint**, connect the GitHub repository, pick the branch, then
   **Apply**. Render creates `opsagents-db` (PostgreSQL 16) and `opsagents` (the web service).
3. Render asks for the secrets marked `sync: false` in `render.yaml`:

   | Secret | What to put |
   |---|---|
   | `LLM_API_KEY` | Your Hugging Face token (`hf_…`) |
   | `CODE_REVIEWER_LLM_API_KEY` | Your xAI key |
   | `XAI_API_KEY` | Your xAI key (Drift Detection) |
   | `GITHUB_TOKEN` | Optional. You can also paste it in **Settings** later. |
   | `LANGSMITH_API_KEY` | Optional, for tracing |

4. Wait for the first build, about 5–10 minutes. Later builds reuse the cached layers.
5. Open the service's **Environment** tab and copy `APP_PASSWORD` (Render generated it).
6. Open `https://opsagents-<something>.onrender.com`, sign in as `opsagents` with that
   password, and start a scenario from **Home**.

To use **Drift Detection**, run `drift-agent init` once in the service's **Shell** tab.
It downloads the dataset and trains the first model.

### Plans and costs

| Piece | Plan in `render.yaml` | Why |
|---|---|---|
| Web service | `starter` (512 MB) | The app peaked at about 310 MB in our test. Use `standard` (2 GB) if Render reports it ran out of memory, for example during drift training. |
| Database | `basic-256mb` | Kept between deploys. The free database plan expires after 30 days. |

The free web plan also works for a quick demo. But it sleeps after 15 minutes without
visits, which stops the GitHub watcher, and it has no Shell tab.

### Run the same image elsewhere

```bash
docker build -t opsagents .
docker run -p 8080:8080 --env-file .env -e DATABASE_URL=postgresql://user:pass@host:5432/agents -e APP_PASSWORD=change-me opsagents
```

The database must be PostgreSQL 16 or newer with the `vector` extension available. Render
Postgres, Neon and the `pgvector/pgvector:pg16` image all have it.

---

## Configuration

Everything is set in `.env` at the repository root. `.env.example` documents every value.

### Shared infrastructure

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | `postgresql://devops:devops@localhost:5432/agents` | Used with `--docker`. The embedded mode sets it for you. |
| `OPA_URL` | `http://localhost:8181` | Policy engine. The embedded mode sets it for you. |
| `OPA_BIN` | – | Path to an OPA binary, if you don't want the automatic download. |
| `KNOWLEDGE_COLLECTION` | `devops_kb` | Knowledge-base collection the RCA agent searches. |
| `EMBEDDING_MODEL` | `BAAI/bge-small-en-v1.5` | Local embedding model (384 dimensions, must match the database). |

### Models

Every agent talks to an OpenAI-compatible endpoint. One default is set for all of them,
and each agent can override it with `<AGENT>_LLM_BASE_URL`, `_API_KEY`, `_MODEL`,
`_TIMEOUT`, `_JSON_MODE` and `_MAX_TOKENS`. `<AGENT>` is `ORCHESTRATOR`, `DIAGNOSTIC_RCA`,
`DEPENDENCY_RESOLVER`, `MERGE_ASSISTANT`, `CODE_REVIEWER` or `GUARDRAIL`.

| Variable | Default | Purpose |
|---|---|---|
| `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` | local Ollama, `llama3.1:8b` | The default for every agent. |
| `LLM_TIMEOUT` | 60 | Seconds per model call. |
| `LLM_RETRIES` | 2 | Retries before falling back to rules. |
| `LLM_COOLDOWN` | 30 | After a failure, skip that model for this many seconds. |
| `XAI_API_KEY` / `XAI_MODEL` / `XAI_BASE_URL` | – | Drift Detection still uses its own xAI settings. |

The line-up the team runs:

| Agent | Model | Provider | Why this model |
|---|---|---|---|
| Orchestrator (triage, summaries) | Llama 3.1 8B Instruct | Hugging Face | Short tasks; the control flow is plain code. |
| Diagnostic / RCA | DeepSeek-R1-Distill-Qwen-7B | Hugging Face | Finding a root cause is multi-step reasoning. |
| Dependency Resolver | Llama 3.1 8B Instruct | Hugging Face | The graph tools find the cycle; the model explains it. |
| Merge Assistant | Llama 3.1 8B Instruct | Hugging Face | A few conflicting lines need no big model. |
| Code Reviewer | Grok 4.20 (non-reasoning) | xAI | Reliable tool calling. The HF coder models lack it. |
| Drift Detection | Grok 4.20 (reasoning) | xAI | Statistics detect the drift; the model explains it. |
| Guardrail judge | Qwen 2.5 7B Instruct | Hugging Face (`:featherless-ai`) | A different model family from the agents it judges; strict JSON. |

Model quirks we hit: DeepSeek-R1 returns empty output in JSON mode, so RCA needs
`JSON_MODE=false` and `MAX_TOKENS=2500`. Qwen 2.5 7B on Hugging Face needs the
`:featherless-ai` suffix.

When a model can't be reached, every agent falls back to rules and the run is marked
*degraded*. The guardrail's rules never depend on an LLM.

### Orchestrator, GitHub and guardrail

| Variable | Default | Purpose |
|---|---|---|
| `APP_PORT` | 8080 | Dashboard and API port. A host's `PORT` takes precedence and makes the app listen on `0.0.0.0`. |
| `APP_PASSWORD` | – | When set, every page and API call needs a sign-in. Set it on any public deployment. |
| `APP_USERNAME` | `opsagents` | The user name for that sign-in. |
| `GITHUB_TOKEN` | – | Optional. You can also paste a token in **Settings**. |
| `GITHUB_POLL_SECONDS` | 60 | How often the watcher polls GitHub. |
| `ORCHESTRATOR_TRIAGE_MIN_CONFIDENCE` | 0.7 | Below this, free-text incidents go to a human. |
| `ORCHESTRATOR_GITHUB_PUBLISH` | false | `true` = really publish bot branches. Not implemented yet, so leave it off. |
| `GUARDRAIL_LLM` | on | `off` skips the LLM judge; the policy still decides. |
| `GUARDRAIL_APPROVAL_TTL_HOURS` | 4 | Approvals expire after this long. |
| `GUARDRAIL_CORS_ORIGINS` | `*` | CORS origins for the guardrail API. |
| `RETRIEVAL_MAX_DISTANCE` | 0.3 | RCA's cosine-distance cutoff for knowledge-base matches. |

### Tracing (optional)

Set `LANGSMITH_TRACING=true` and `LANGSMITH_API_KEY` (a personal access token from
https://smith.langchain.com) to get one LangSmith trace per run, with every agent step and
model call nested inside it. `LANGSMITH_PROJECT` defaults to `opsagents`. Without the key,
tracing is off and nothing else changes.

---

## Using the dashboard

The dashboard is a static page in `web/`, with no build step. Its pages:

| Page | What it shows |
|---|---|
| **Home** | Repository health, the demo scenarios and a box to describe an incident in plain text. |
| **Activity** | Every commit and pull request with its CI status. Investigate any failure in one click. |
| **Runs** | Each investigation: its route, agent steps, proposals, guardrail decisions and timeline. |
| **Approvals** | Held proposals. Approve or reject them as the user picked in **Acting as**. |
| **Triage** | Incidents the rules and the LLM couldn't route. A human picks the route. |
| **Alerts** | CI failures, recoveries, approvals needed and agents unavailable. Desktop notifications if you allow them. |
| **Analytics** | Runs, decisions, review findings, CI pass rate and recovery time, per agent. |
| **Agent registry** | Each agent's model, provider and health. |
| **Audit log** | Every decision, with a check of the hash chain. |
| **Repositories / Settings** | GitHub sign-in, followed repositories, polling and auto-investigate. |

Add `?mock=1` to the URL for a fake backend (handy for UI work), and `&gh=signedout` or
`&gh=setup` to preview the sign-in screens.

### Demo scenarios

Start any of these from **Home**. No GitHub account is needed.

| Scenario | Route | What happens |
|---|---|---|
| Failed deploy: IAM cycle | `deploy_failed` | `tofu apply` fails on a cycle between an IAM role, its policy and an EKS cluster. The Dependency Resolver finds the cycle and proposes a fix. The guardrail raises the risk to high because of IAM and holds it for one human. |
| Failed CI build | `build_failed` | pytest can't reach the database. RCA finds the missing `DATABASE_URL` and cites the knowledge-base document. |
| Review a pull request | `pull_request` | A data scientist's PR fits the scaler before the train/test split. The Code Reviewer reports 6 findings, 4 of them high severity. |
| Merge two branches | `merge_requested` | `feature-a` and `feature-b` both change `TIMEOUT` in `config.py`. The Merge Assistant finds the conflict and proposes a resolution. It never merges. |
| Check model drift | `drift_check` | Drift Detection runs on the next production batch. Unavailable until its data package is pushed (see [Known gaps](#known-gaps)). |

Typing an incident in plain text (for example *"the nightly pipeline is red, pytest says
connection refused"*) goes through the same routing as everything else: rules first, then
the LLM, then a human.

### Approving a proposal

1. Open **Approvals**, or the run itself.
2. Pick who you are in **Acting as** (bottom left).
3. Click **Approve** or **Reject**.

The guardrail enforces the rules. You can't approve a run you triggered yourself (403),
you can't vote twice (409), approvals expire after 4 hours, and production plus high risk
needs two different people.

---

## Connect GitHub

You can connect with a personal access token or with a GitHub sign-in.

**Token (simplest).** Create a fine-grained token with **read** access to *Contents*,
*Actions*, *Checks* and *Pull requests* on the repositories you want to follow. Paste it in
**Settings**, or set `GITHUB_TOKEN` in `.env`.

**Sign in with GitHub (device flow).**
1. Create a GitHub OAuth App with Device Flow enabled. Use `http://localhost:8080` as the
   homepage and the callback URL.
2. Paste its Client ID in **Settings**.
3. Click **Sign in with GitHub** and enter the code GitHub shows you.

Then add repositories from **Repositories**. The watcher:

- polls GitHub every 60 s, because webhooks can't reach a laptop;
- records existing history without alerting;
- alerts on each new failure exactly once, and on recoveries.

With **auto-investigate** on:
- a failed CI run starts RCA on the real job log, with secrets removed;
- a failed OpenTofu/Terraform job goes to the Dependency Resolver with the commit's `.tf` files;
- a new pull request gets a code review.

### Try it live

The repository [`Kreemy29/opsagents-demo`](https://github.com/Kreemy29/opsagents-demo) runs
OpenTofu (`init`, `validate`, `graph`) on every push, and its `main` contains the IAM
cycle. Follow it in OpsAgents, push any commit, and within about a minute you will see:
the red CI alert, a *Failed deployment* run, the cycle found, and the fix held for approval.

---

## How it works

### The flow of one run

1. **Trigger.** A GitHub event (failed CI, failed deploy, new PR), a demo scenario or a
   free-text incident.
2. **Routing** (`agents/orchestrator/routing.py`):
   - *Rules*: known trigger types and keywords map straight to a route, with confidence 1.0.
   - *LLM triage*: otherwise the model picks a route with a confidence. It is accepted at 0.7
     or more and only if it names a known route.
   - *Human triage*: low confidence, an invalid answer or no LLM. The run waits in **Triage**.
     The system never guesses.
3. **Agents.** The route's agents run in order. A step can depend on an earlier result: for
   example, RCA runs before the Code Reviewer only if the PR's tests failed.
4. **Proposals.** Each agent publishes proposals (`propose_fix`, `rank_causes`,
   `review_code`, …) as events. Agents never change anything themselves.
5. **Guardrail.** Each proposal is auto-approved, held for humans or rejected (see
   [The guardrail](#the-guardrail)).
6. **Execution.** Only after approval, and only by the orchestrator's executors.
7. **Summary and alerts.** The LLM writes a short summary, and the run finishes with an alert.

### Routes

| Route | Trigger | Agents, in order |
|---|---|---|
| `build_failed` | A CI build or test run failed | Diagnostic / RCA |
| `deploy_failed` | An OpenTofu/Terraform deployment failed | Dependency Resolver → Diagnostic / RCA |
| `merge_requested` | Two branches must be merged | Merge Assistant |
| `pull_request` | A PR was opened or updated | Diagnostic / RCA (only if tests failed) → Code Reviewer |
| `drift_check` | Check the deployed model for drift | Drift Detection → Diagnostic / RCA (only if it's a pipeline bug) |

### How information flows

- **One run ID on everything:** events, approvals, audit entries and traces all carry it.
- **Facts pass forward:** for example, RCA's result is handed to the Code Reviewer.
- **One event bus, in PostgreSQL** (`shared/events.py`): a failed event is retried with a
  growing delay, and it is marked *dead* after 5 attempts.
- **Resumable:** the orchestrator is a deterministic state machine stored in PostgreSQL. If
  the app crashes, runs resume where they stopped.
- **One transaction:** an action and its event are written together, so they can't
  disagree.

### Main technical choices

1. **Deterministic orchestrator.** The control flow is plain code, not an LLM, so runs are
   repeatable and can resume after a crash.
2. **Policy as code.** The approval rules are in Rego (`agents/guardrail/policy/`), with 20
   tests.
3. **One PostgreSQL database.** It holds the records, the event bus, the knowledge-base
   vectors and the drift data.
4. **Graceful degradation.** Every agent has a rule-based fallback, and runs are marked
   *degraded* instead of failing.
5. **The statistics and graph tools decide; the LLMs explain.**

---

## The agents

| Agent | Owner | Goal | Model | Tools |
|---|---|---|---|---|
| Orchestrator | Rayen | Runs each trigger end to end, recoverably | Llama 3.1 8B | Route rules, LLM triage, agent adapters, event bus, executors, alerts |
| Diagnostic / RCA | zrharounesprit | Explains why a build failed | DeepSeek-R1-Distill-Qwen-7B | Log cleaner, pgvector knowledge base, few-shot examples, citation validator, rule-based fallback |
| Dependency Resolver | Lyna | Unblocks deploys stuck on circular dependencies | Llama 3.1 8B | `tofu graph`, networkx, pydot, static `.tf` parser, keyword classifier |
| Merge Assistant | Muaad | Finds conflicts before merging, proposes a fix, **never merges** | Llama 3.1 8B | GitPython scratch clone, trial merge, conflict extractor |
| Code Reviewer | hendch | Reviews PRs for ML/MLOps mistakes, proposes safe lint fixes | Grok 4.20 non-reasoning | `list_changed_files`, `get_diff`, `get_lint_findings`, `get_test_failures`, `submit_review`, ruff |
| Drift Detection | omar | Spots data and concept drift, explains it, recommends an action | Grok 4.20 reasoning | KS test, PSI, chi-square, feature importance, model registry |
| Guardrail | Rayen | No agent makes a risky change on its own | Qwen 2.5 7B | Risk scanner, OPA (Rego), LLM judge, approvals store, hash-chained audit log |

Each agent's own folder has more detail: `agents/code_reviewer/README.md`,
`agents/drift_detection/README.md` and `agents/guardrail/README.md`.

### Running an agent by hand

The orchestrator calls each agent through `agents/orchestrator/adapters.py`. Some can also
be run on their own:

```bash
python -m agents.guardrail.playground          # guardrail playground on http://localhost:8090
python -m agents.diagnostic_rca.seed_dev_kb    # seed the RCA development knowledge base
drift-agent --help                             # drift detection CLI (needs pip install -e .)
python demo/run_local_demo.py                  # code-review demo without the dashboard
```

---

## The guardrail

Each proposal goes through three checks, in order:

1. **Risk from evidence** (`agents/guardrail/risk.py`). The guardrail recomputes the risk
   from what the proposal touches. It can only **raise** the agent's own label, never lower
   it:

   | Signal | Raises risk to |
   |---|---|
   | `prod`, `production` | high |
   | `destroy`, `delete`, `drop table`, `truncate`, `force-unlock`, `rm -rf`, `terminate` | high |
   | `iam`, `kms`, `secret`, `security_group`, `firewall`, role definitions | high |
   | GPU instances, NAT gateways, RDS, EKS clusters, autoscaling | medium |
   | `backend`, `tfstate`, `state lock` | medium |

   Output from a fallback (no LLM, no OpenTofu) counts as less reliable.

2. **Policy** (`agents/guardrail/policy/guardrail.rego`). OPA applies the first matching
   rule:

   | Rule | Decision |
   |---|---|
   | `agent_not_allowed_action` | Reject: the agent isn't allowed this action. |
   | `prod_high_two_person` | Hold: production and high risk need 2 approvals. |
   | `high_always_human` | Hold: high risk always needs a human, whatever the environment. |
   | `advisory_only` | Auto-approve: advice only (`rank_causes`, `review_code`, `drift_report`). |
   | `prod_requires_human` | Hold: any production change needs a human. |
   | `staging_medium_human` | Hold: medium risk in staging needs a human. |
   | `low_risk_auto` | Auto-approve: low risk. |
   | `dev_medium_auto` | Auto-approve: medium risk in dev. |
   | `default_hold` | Hold: no rule matched, so a human decides (fail closed). |

3. **LLM judge** (`agents/guardrail/llm_judge.py`). A second model, from a different family,
   reads the proposal. It can only make the decision **stricter**, never looser.

Other protections:
- **Fail closed:** if the policy engine is down, everything is held for a human.
- **Injection-safe:** text written by agents, commits or CI logs is treated as data, never
  as instructions.
- **Approvals:** no self-approval (403), no double votes (409), and approvals expire after
  4 hours.
- **Audit:** every decision goes to an append-only, hash-chained audit log. **Audit log**
  checks the chain.
- **Dry runs:** GitHub actions are dry runs, and nothing is ever merged.

---

## Data

| Data | Used by | What it is |
|---|---|---|
| CI/CD documentation | RCA | A knowledge base of 5 documents (`agents/diagnostic_rca/seed_dev_kb.py`), embedded in pgvector. `corpus/ingest.py` loads more. |
| Failed build logs | RCA | A sample pytest log with secrets removed (`agents/diagnostic_rca/logs/`). |
| OpenTofu configurations | Dependency Resolver | Demo infrastructure with an IAM cycle (`demo/infra/iam_cycle/`). |
| Pull requests | Code Reviewer | Demo PRs with data leakage and side effects (`demo/scenarios/`, `ml_demo/`). |
| UCI Bank Marketing | Drift Detection | 45,211 rows, replayed batch by batch: 45% training, 20% reference, the rest is the production pool. |

The database schema is in `db/migrations/`:

| File | Tables |
|---|---|
| `001_core.sql` | Shared records |
| `002_guardrail.sql` | Proposals, decisions, approvals, audit chain |
| `003_events.sql` | Event bus and per-consumer receipts |
| `004_orchestrator.sql` | Runs, steps, triage |
| `005_knowledge.sql` | Knowledge-base documents and vectors |
| `006_github_alerts.sql` | Followed repositories, GitHub activity, alerts |

`python -m shared.migrate` applies them. It is safe to run more than once.

---

## Tests

```bash
python -m pytest                 # every test: platform + each agent's own tests
python -m pytest tests           # platform only (orchestrator, shared, tracing)
python -m pytest agents/guardrail
opa test agents/guardrail/policy -v   # the 20 policy tests (OPA is in .tools/ after the first start)
```

The tests run on a throwaway PostgreSQL and the real OPA policy. They need no Docker and
no LLM; the model calls are mocked.

| Component | Tests | What they check |
|---|---|---|
| Guardrail | 48 | Risk scoring, approvals, malformed events, self-approval, audit chain |
| Code Reviewer | 27 | Safety limits, patch validation, the LLM loop, an end-to-end review |
| Drift Detection | 30 | Statistical tests (PSI, KS, chi-square) and the PostgreSQL store. 12 are integration tests: they need `drift-agent init` and the database in `DATABASE_URL`. |
| Dependency Resolver | 12 | Static parser and classifier fallbacks |
| Orchestrator, GitHub, API, platform | 53 | Runs complete or wait for approval; a GitHub failure raises one alert and one investigation |
| OPA policy | 20 | Each rule and the order the rules apply in |

The counts above are from the prototype validation (20 Oct 2026). Last full run, 2 Nov 2026:
**pytest 160 passed; OPA 20/20.** The 12 drift integration tests errored: with the data
package now in the repository they run instead of being skipped, but they need a PostgreSQL
at `DATABASE_URL` that `drift-agent init` has been run against. Screenshots are in
`docs/test-results/`, and each test is explained in `docs/OpsAgents-Tests-Explained.docx`.

A separate benchmark of 64 cases, with live models against a rules-only baseline, is in
the `opsagents-bench` repository.

---

## API

The dashboard uses a JSON API served by FastAPI on the same port. Interactive docs are at
http://localhost:8080/docs.

| Method | Path | What it does |
|---|---|---|
| GET | `/api/health` | Health of the database, OPA, the models and the agents |
| GET | `/api/overview` | Home-page summary |
| GET | `/api/scenarios` · `/api/routes` | Demo scenarios and routes |
| POST | `/api/runs` | Start a run (scenario, route + params, or free text) |
| GET | `/api/runs` · `/api/runs/{id}` | List runs, or one run with steps, proposals and timeline |
| POST | `/api/runs/{id}/triage` | Pick a route for a run waiting in triage |
| GET | `/api/approvals` | Held proposals |
| POST | `/api/approvals/{id}/approve` · `/reject` | Vote on a proposal |
| GET | `/api/agents` | Agents, models and health |
| GET | `/api/audit` | Audit log, with the hash-chain check |
| GET / POST | `/api/alerts` · `/api/alerts/read` | Alerts, and marking them read |
| GET | `/api/analytics` | Analytics per agent |
| GET | `/api/github/status` · `/repos` · `/activity` | GitHub connection, followed repositories, activity |
| POST | `/api/github/token` · `/oauth/*` · `/disconnect` | Connect or disconnect GitHub |
| POST / DELETE | `/api/github/repos` · `/repos/{owner}/{name}` | Follow or unfollow a repository |
| POST | `/api/github/sync` · `/investigate` · `/settings` | Poll now, investigate a failure, change options |

---

## Project layout

```
app/                    python -m app: launcher (__main__.py) and the platform API (api.py)
web/                    the dashboard: index.html, app.js, app.css (static, no build step)
agents/
  orchestrator/         routing, run engine, adapters, executors, GitHub watcher, analytics
  guardrail/            risk scan, OPA policy, LLM judge, approvals, audit log, playground
  dependency_resolver/  circular dependencies in OpenTofu code (+ static fallback)
  diagnostic_rca/       root causes of failed builds (RAG on pgvector)
  merge_assistant/      merge conflicts and proposed resolutions
  code_reviewer/        ML/MLOps review of pull requests, safe lint fixes
  drift_detection/      data and concept drift on a deployed model
shared/                 database, event bus, knowledge base, LLM client, redaction,
                        alerts, tracing, migrations, embedded dev stack
db/migrations/          numbered SQL files (python -m shared.migrate)
corpus/                 knowledge-base documents for RCA
demo/                   demo scenarios: PRs, IAM-cycle infrastructure, scripted runs
ml_demo/                small ML project targeted by the seeded demo pull request
tests/                  platform tests; each agent keeps its own tests in its folder
docs/                   report, validation report, test results and explanations
requirements.txt        every package, pinned
pyproject.toml          the same packages as an installable project
docker-compose.yml      persistent PostgreSQL + OPA
.env.example            every setting, documented
```

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `pip install` fails on `psycopg2-binary` or `onnxruntime` | Check that you're on 64-bit Python 3.12+, then upgrade pip: `python -m pip install --upgrade pip`. |
| The first start hangs on "downloading OPA" | You need internet access to GitHub. Or download OPA 1.20.2 yourself and set `OPA_BIN` to its path. |
| Port 8080 is in use | `python -m app --port 8081` |
| A run is marked *degraded* | A model or OpenTofu was unreachable and the agent used its fallback. Check **Agent registry** and your `.env` keys. |
| RCA returns nothing | Set `DIAGNOSTIC_RCA_LLM_JSON_MODE=false` and `DIAGNOSTIC_RCA_LLM_MAX_TOKENS=2500`. |
| `Qwen/Qwen2.5-7B-Instruct` model not found | Add the provider suffix: `Qwen/Qwen2.5-7B-Instruct:featherless-ai`. |
| Hugging Face "Connection error" | The router sometimes has outages. Wait or switch provider. The agents fall back to rules meanwhile. |
| Blank dashboard after an update | Hard-reload the page (Ctrl+F5): the browser kept an old `app.js`. |
| Leftover `postgres` processes after killing the app | They are cleaned up on the next start. |
| A tunnel gives 502 errors | Point it at `127.0.0.1:8080`, not `localhost:8080`. |
| Drift Detection shows *unavailable* | Its data package isn't in the repository yet (see below). |

---

## Known gaps

- **Drift Detection needs one setup step:** run `drift-agent init` once, which downloads the
  dataset and trains the first model.
- **No per-person sign-in.** `APP_PASSWORD` locks the whole app behind one shared password,
  but "Acting as" is still a dropdown and CORS is `*`. The next step is to tie approvers to
  their GitHub sign-in.
- **Nothing is written back to GitHub.** Patches are dry runs. Pushing a bot branch,
  opening a draft PR and posting review comments are not implemented yet.
- **Polling only, no webhooks.** The Merge Assistant and Drift Detection aren't triggered
  from GitHub yet.
- **The RCA knowledge base is small:** 5 sample documents. On cases it doesn't cover, RCA
  can cite an unrelated document. Fill it with real runbooks with `corpus/ingest.py`.
- **OpenTofu isn't bundled.** Without it, the Dependency Resolver uses its static parser.
- **code_reviewer workflow:** `.github/workflows/code-review-agent.yml` still checks out the
  agent from the `code-reviewer` branch and runs `pytest` at the repository root. Scope it
  to `ml_demo/` once merged.
- **One instance only.** The orchestrator and the GitHub watcher run inside the web process,
  so don't scale the Render service beyond one instance.

## Team

| Member | Part |
|---|---|
| Rayen | Guardrail, orchestrator, platform integration and dashboard |
| Lyna | Dependency Resolver |
| zrharounesprit | Diagnostic / RCA |
| Muaad | Merge Assistant |
| hendch | Code Reviewer |
| omar | Drift Detection |

Data Science Project, 2026.
