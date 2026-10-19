# Drift Detection Agent

One agent of our multi-agent MLOps system. It watches a production model, detects when the data or
the model's accuracy drifts, investigates **why** with an LLM (Grok) using read-only tools, and acts
through a guardrail: safe actions run automatically, anything that changes the live model waits for a
human.

Statistics decide **whether** something drifted. The LLM explains **why**. A human decides **what
changes in production**.

## Quick start

Requires [uv](https://docs.astral.sh/uv/) (it installs Python 3.12 for you).

```bash
cd agents/drift_detection   # this folder, from the repo root
uv sync                      # install dependencies
uv run drift-agent init      # download the dataset, train model v1 (~10 s)
uv run drift-agent serve     # dashboard on http://127.0.0.1:8000
```

In the dashboard: pick a scenario, press **Restart**, then **Next batch** (or switch on **Live**).

### Using Grok

Without a key the agent uses its rule-based investigator, so everything works offline. To use Grok:

```bash
cp .env.example .env         # then paste your key from https://console.x.ai into XAI_API_KEY
uv run drift-agent llm check # verifies the key and lists the model names you can use
```

Set `XAI_MODEL` in `.env` to one of the listed models. The key is read on every investigation, so
adding or changing it needs no restart. If Grok fails (network, quota, invalid answer), the run
falls back to the rule-based investigator and the report says so.

## Configuration

Everything tunable lives in `config.yaml`: dataset source, model hyperparameters, scenario
parameters, every detection threshold, the agent's tool limits, alert routing, server and
dashboard settings. The dashboard reads its thresholds from `GET /api/config`, so it always
matches the backend. Secrets live only in `.env`.

## Demo script (about 3 minutes)

| Scenario | What happens | What the agent should conclude |
|---|---|---|
| `pipeline_bug` | From batch 8 an "ETL release" multiplies `balance` by 100, blanks 30% of `job`, and sends an unseen `contact = whatsapp` | Critical at once. **Pipeline bug**, investigate upstream, **do not retrain** |
| `gradual` | The customer base gets older each batch | Alert after a few batches. **Population shift**, accuracy holds, so **monitor** |
| `concept` | Inputs unchanged, but subscription behaviour changes for clients with a housing loan | Caught when labels arrive (2 batches late). **Concept drift**, a challenger is trained, a **human approves** the promotion |
| `clean` | Nothing | No alerts |

From the command line instead:

```bash
uv run drift-agent reset --scenario concept
uv run drift-agent simulate -n 14
uv run drift-agent report            # the latest incident report
uv run drift-agent approvals         # what is waiting for a human
uv run drift-agent approve 3         # promote the challenger (use the id shown)
uv run drift-agent rollback          # undo: requests a rollback, which also needs approval
```

## How it works

```
 production batch ──► Detector (statistics) ──► severity OK / WARNING / ALERT / CRITICAL
                                                    │ ALERT or CRITICAL
                                                    ▼
                      Investigator (Grok + read-only tools, or rule-based fallback)
                                                    │ incident report + recommended action
                                                    ▼
                      Guardrail ──► auto: raise alert, train challenger in sandbox
                                └─► needs a human: promote model, roll back
                                                    │
                                   everything ──► audit log (SQLite)
```

### 1. Detection (`detection/`)

Each batch is compared with the reference snapshot of the model's training-time data:

| Check | Numeric features | Categorical features |
|---|---|---|
| Is it different? | Kolmogorov-Smirnov test (p < 0.05) | Chi-square test (p < 0.05) |
| How different? | KS distance D ≥ 0.10 | Jensen-Shannon distance ≥ 0.10 |
| Magnitude | PSI (0.10 moderate, 0.25 significant) | PSI on category shares |

Requiring **both** significance and effect size avoids flagging tiny differences that are only
"significant" because the batch is large. Also checked: schema, missing-value spikes, unseen
categories, out-of-range values, prediction drift (PSI on scores, predicted yes-rate) and ROC-AUC on
the newest labelled batches.

False alarms are controlled by a **persistence rule** (a moderate shift must also appear in the
previous batch) and by **weighting each feature by its permutation importance**, so drift in a
feature the model barely uses barely moves the score. All thresholds are in `config.yaml`.

### 2. Investigation (`agent/`)

Grok gets the detector's findings and 8 **read-only** tools: `get_drift_report`,
`compare_distributions`, `segment_breakdown`, `feature_importance`, `drift_history`,
`recent_pipeline_events`, `performance_history` and `model_info`. It must return a JSON incident
report (validated with pydantic): drift type, ranked root causes with **evidence citing the tools
it called**, impact, and one action from a fixed list. Evidence citing tools it never called is
flagged as unverified. The loop is capped at 8 tool calls.

### 3. Guardrail (`guardrail/`)

| Action | Risk | Mode |
|---|---|---|
| Raise an alert | none | automatic |
| Train a challenger model and compare it with the current one on held-out recent batches | low | automatic |
| Promote the challenger | high | **human approval** |
| Roll back to the previous version | high | **human approval** |

There is no code path for anything else. Old model versions are kept, so every promotion can be undone.

## Evaluation

`uv run drift-agent evaluate` runs every scenario with several seeds in isolated workspaces and
writes `eval/results/latest.md`. Add `--llm` to benchmark Grok instead of the rule-based
investigator. Rule-based results (3 seeds, 20 batches, drift from batch 8, labels 2 batches late):

| Scenario | Detection rate | False alarm rate | Time to detect | Diagnosis accuracy | Action accuracy | Challenger AUC gain |
|---|---|---|---|---|---|---|
| clean | - | 0% | - | 100% | 100% | - |
| gradual | 100% | 0% | 3.0 batches | 100% | 100% | - |
| pipeline_bug | 100% | 0% | 0.0 batches | 100% | 100% | - |
| concept | 100% | 0% | 3.7 batches | 100% | 100% | +0.050 |

With Grok (`grok-4.3`) as the investigator (1 seed): detection, false alarms and diagnosis were
identical, 100% correct on every scenario, with all three diagnoses written by Grok.

Concept drift is slower by design: it can only be seen once true labels arrive.

## Data

[UCI Bank Marketing](https://archive.ics.uci.edu/dataset/222/bank+marketing): 45,211 phone-campaign
calls, target = did the client subscribe to a term deposit (11.7% yes). `duration` is dropped because
it is only known after the call, which would leak the answer. Because of the class imbalance the model
is judged on ROC-AUC and F1, not accuracy. Baseline: ROC-AUC 0.795, F1 0.48.

Split: 45% training, 20% reference snapshot, 35% production pool the simulator samples from.

## API

`uv run drift-agent serve` then open `/docs` for interactive documentation. Main endpoints:
`GET /api/status`, `GET /api/runs`, `GET /api/runs/{id}`, `POST /api/check`,
`POST /api/simulate/step`, `POST /api/simulate/reset`, `GET /api/approvals`,
`POST /api/approvals/{id}/approve`, `POST /api/approvals/{id}/reject`, `POST /api/models/rollback`,
`GET /api/audit`.

## Project layout

```
config.yaml              every threshold
src/drift_agent/
  data/                  dataset download + production simulator with drift scenarios
  model/                 training, evaluation, versioned registry
  detection/             statistics, quality checks, severity
  agent/                 Grok client, tools, prompts, investigation loop, rule-based fallback
  guardrail/             risk policy, challenger training, approvals
  store/                 SQLite (runs, reports, actions, models, audit log)
  api/                   FastAPI + dashboard (HTML, CSS, Chart.js)
  service.py             wires everything; used by CLI, API, scheduler, evaluation
  evaluation.py          scenario benchmark
tests/                   statistics unit tests + end-to-end scenario tests
```

Run the tests with `uv run pytest`.

## Limitations

- Features are tested one at a time; drift that only appears in feature *combinations* can be missed
  (next step: a classifier trained to tell reference rows from current rows).
- Production is simulated; there is one model, one user and no login on the dashboard (local use only).
- The LLM can still reason wrongly. That is why its tools are read-only, its evidence must cite real
  tool output, and a human approves every change to the live model.
