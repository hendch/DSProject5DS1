# Code Review and Minimal Fix Agent

Reviews a pull request, reports ML/MLOps problems with evidence, fixes only a few
safe lint issues (unused imports, import order, trailing whitespace) with `ruff`,
validates the result, and opens **one draft PR** for a human. It never merges.

Standalone: it does not import, call, or change any other agent in this repository.

## How it runs

```
PR opened/updated ──► GitHub Actions
                        │
   job "review"  (self-hosted runner on the VPS, read-only token, LLM key)
     checkout PR head (no credentials) + agent code from AGENT_REF
     baseline ruff + pytest ──► agent.py: LLM review ─► ruff fixes ─► guard ─► validate
     upload agent-out/ (report.json, report.md, fix.patch)
                        │
   job "publish" (GitHub-hosted runner, write token, no LLM key, runs no repo code)
     sticky comment on the PR; if repaired: push bot/minimal-fix/pr-<N> + draft PR
```

| File | Responsibility |
|---|---|
| `context.py` | Diff, changed files, lint/test reports, rules → `ReviewContext` |
| `rules.py` | Mandatory rules (bundled) + `.review/rules/*.md` from the **base** commit; `get_rules()` retrieval interface |
| `tools.py` | Read-only tools for the LLM; `request_safe_fix` only queues a checked request |
| `llm.py` | Bounded tool-calling loop over any OpenAI-compatible endpoint |
| `fixer.py` | `ruff check --isolated --select <allowed> --fix` (safe fixes only) |
| `guard.py` | Only allowed files, no protected paths, size cap, AST unchanged except imports, lint codes gone |
| `validate.py` | Commands from `policy.yaml` (ruff + pytest) |
| `report.py` | `report.json` and the markdown comment / PR body |
| `publish.py` | Comment, push bot branch, open/update draft PR. Only code with a write token |
| `skill/minimal_fix.md` | What to review and how to report (the prompt) |
| `policy.yaml` | What is allowed (enforced in code, not by the prompt) |

### How GitHub triggers the VPS

GitHub never connects to the VPS. The Actions runner service on the VPS keeps an
outbound HTTPS connection to GitHub and picks up jobs whose `runs-on` labels match
its own (`self-hosted, linux, code-review-agent`). No inbound ports are needed.

### Why it stays independent of teammates' branches

- For `pull_request` events, GitHub uses the workflow file from the PR's merge commit.
  The workflow exists only on `code-reviewer` (and branches made from it), so PRs
  on teammates' branches never run it.
- The agent code is checked out from `AGENT_REF` (default `code-reviewer`), never from
  the PR being reviewed. A PR cannot change the agent's own policy or prompt.
- The runner label `code-review-agent` is used only by this workflow. Ask teammates
  not to use a bare `runs-on: self-hosted`, which would also match the VPS runner.

## One-time setup

### 1. VPS runner (Debian)

```bash
sudo apt-get install -y git python3 python3-venv curl
sudo useradd -m -s /bin/bash gha-runner          # unprivileged user, no sudo
sudo -iu gha-runner
mkdir actions-runner && cd actions-runner
# Download + token: repo → Settings → Actions → Runners → New self-hosted runner → Linux x64
./config.sh --url https://github.com/hendch/DSProject5DS1 --token <TOKEN> \
  --name vps-code-review --labels code-review-agent --unattended
exit
cd /home/gha-runner/actions-runner && sudo ./svc.sh install gha-runner && sudo ./svc.sh start
```

The repo must stay **private**. Fork PRs are skipped by the workflow, but a self-hosted
runner on a public repo is still a risk.

### 2. Repository settings

- **Settings → Secrets and variables → Actions**
  - Secret `LLM_API_KEY`: provider API key
  - Variable `LLM_BASE_URL`: `https://openrouter.ai/api/v1`
  - Variable `LLM_MODEL`: `qwen/qwen3-coder`
  - Optional: `LLM_PRICE_IN_PER_MTOK`, `LLM_PRICE_OUT_PER_MTOK` (adds cost to reports), `AGENT_REF`
- **Settings → Actions → General → Workflow permissions**: enable
  "Allow GitHub Actions to create and approve pull requests" (needed to open the draft PR).
- **Branch protection on `main` and `code-reviewer`**: require a pull request review.

### Loop and duplicate protection

- Pushes made with the workflow's `GITHUB_TOKEN` do not start new workflow runs.
- The review job also skips `bot/*` branches and the `github-actions[bot]` actor.
- One fixed branch per PR (`bot/minimal-fix/pr-<N>`); an open PR for it is updated, not duplicated.
- `publish.py` refuses any push target outside `refs/heads/bot/minimal-fix/`.
- One repair attempt. If the guard or validation fails, everything is reverted and reported.

Known limitation: because of the `GITHUB_TOKEN` rule, CI does not run automatically on
the bot's draft PR. Its body contains the validation results; a human can re-run checks.

## Run locally

```bash
pip install -r code_review_agent/requirements.txt scikit-learn
python demo/run_local_demo.py --scenario leaky_pr          # offline (scripted review)
python demo/run_local_demo.py --scenario side_effect_pr    # fix breaks a test -> reverted
LLM_BASE_URL=... LLM_MODEL=... LLM_API_KEY=... python demo/run_local_demo.py --llm
python -m pytest code_review_agent/tests -q
```

| Scenario | Seeded problems | Expected |
|---|---|---|
| `leaky_pr` | unused imports, unsorted imports, trailing whitespace, scaler fitted before split, no seed, training-set accuracy, prompt-injection comment | `repaired`: F401/I001/W291 fixed; 4 findings reported, logic untouched |
| `side_effect_pr` | import used only for its side effect | `validation_failed`: fix reverted, test output reported |

## Local inference later

The agent talks to any OpenAI-compatible server, so switching is only a variable change:
`LLM_BASE_URL=http://localhost:11434/v1`, `LLM_MODEL=<ollama tag>`, `LLM_API_KEY=ollama`.
On the current VPS (15 GiB RAM, no GPU), `qwen3-coder:30b` (19 GB download) will not
fit in memory; a smaller Qwen3 model fits but will be slower and weaker. Run the same
demo scenarios against both before switching.
