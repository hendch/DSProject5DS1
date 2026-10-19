# Code Review and Minimal Fix Agent

Reviews a pull request, reports ML/MLOps problems with evidence, fixes only a few
safe lint issues (unused imports, import order, trailing whitespace) with `ruff`,
validates the result, and opens **one draft PR** for a human. It never merges.

Standalone: it does not import, call, or change any other agent in this repository.

## How it runs

```
PR opened/updated ──► GitHub Actions (GitHub-hosted runners, nothing to install)
                        │
   job "review"  (read-only token + Claude subscription token)
     checkout PR head (no credentials) + agent code from AGENT_REF
     baseline ruff + pytest ──► agent.py: one Haiku call ─► ruff fixes ─► guard ─► validate
     upload agent-out/ (report.json, report.md, fix.patch)
                        │
   job "publish" (write token, no model token, runs no PR code)
     sticky comment on the PR; if repaired: push bot/minimal-fix/pr-<N> + draft PR
```

| File | Responsibility |
|---|---|
| `context.py` | Diff, changed files, lint/test reports, rules → `ReviewContext` |
| `rules.py` | Mandatory rules (bundled) + `.review/rules/*.md` from the **base** commit; `get_rules()` retrieval interface |
| `llm.py` | The only model code. `claude-cli` backend (default) or `openai` backend |
| `tools.py` | Checks every fix request and the review schema (and serves read-only tools to the `openai` backend) |
| `fixer.py` | `ruff check --isolated --select <allowed> --fix` (safe fixes only) |
| `guard.py` | Only allowed files, no protected paths, size cap, AST unchanged except imports, lint codes gone |
| `validate.py` | "Nothing broke" checks from `policy.yaml` (ruff syntax/undefined-name rules + pytest) |
| `report.py` | `report.json` and the markdown comment / PR body |
| `publish.py` | Comment, push bot branch, open/update draft PR. Only code with a write token |
| `skill/minimal_fix.md` | What to review and how to report (the prompt) |
| `policy.yaml` | What is allowed (enforced in code, not by the prompt) |

### The model call (Claude subscription)

The agent runs Claude Code in headless mode once per PR:

```
claude -p --model claude-haiku-5-5 --tools "" --strict-mcp-config --setting-sources "" \
          --output-format json --json-schema <review schema> --system-prompt <skill>
```

- `--tools ""`: the model has no tools at all (no shell, no file access, no edits). It
  only reads the prompt (diff, changed files, lint/test reports, rules) and returns JSON.
- It runs in an empty temp directory with no settings, so a `CLAUDE.md` or settings
  file in the PR cannot change its instructions.
- Its environment holds only `CLAUDE_CODE_OAUTH_TOKEN` and basics, never GitHub tokens.
  `shell.py` strips the token again before ruff/pytest run the PR's code.
- The answer's `fix_requests` and `findings` go through the same checks as before.
- Usage comes out of your Claude plan's limits, not API billing.

### Why it stays independent of teammates' branches

- For `pull_request` events, GitHub uses the workflow file from the PR's merge commit.
  The workflow exists only on `code-reviewer` (and branches made from it), so PRs on
  teammates' branches never run it.
- The agent code is checked out from `AGENT_REF` (default `code-reviewer`), never from
  the PR being reviewed. A PR cannot change the agent's own policy or prompt.

## One-time setup (repo admin, no server needed)

1. **Create the subscription token** on your own computer:
   ```bash
   claude setup-token
   ```
   It prints a one-year token. Treat it like a password.
2. **Store it in the repo**: Settings → Secrets and variables → Actions → New repository
   secret → name `CLAUDE_CODE_OAUTH_TOKEN`, paste the token.
3. **Allow the bot to open PRs**: Settings → Actions → General → Workflow permissions →
   enable "Allow GitHub Actions to create and approve pull requests".
4. **Protect branches**: require a pull request review on `main` and `code-reviewer`.
5. Optional variables: `LLM_MODEL` (default `claude-haiku-5-5`), `AGENT_REF`.

Every review runs on your subscription, including PRs opened by teammates. Plan limits
assume ordinary individual use, which fits a class project's PR volume.

### Loop and duplicate protection

- Pushes made with the workflow's `GITHUB_TOKEN` do not start new workflow runs.
- The review job also skips `bot/*` branches, the `github-actions[bot]` actor and fork PRs.
- One fixed branch per PR (`bot/minimal-fix/pr-<N>`); an open PR for it is updated, not duplicated.
- `publish.py` refuses any push target outside `refs/heads/bot/minimal-fix/`.
- One repair attempt. If the guard or validation fails, everything is reverted and reported.

Known limitation: because of the `GITHUB_TOKEN` rule, CI does not run automatically on
the bot's draft PR. Its body contains the validation results; a human can re-run checks.

## Run locally

Needs Claude Code installed and logged in (`claude`), for the `--llm` runs.

```bash
pip install -r code_review_agent/requirements.txt scikit-learn
python demo/run_local_demo.py --scenario leaky_pr          # offline, scripted review
python demo/run_local_demo.py --scenario leaky_pr --llm    # real Haiku via your subscription
python demo/run_local_demo.py --scenario side_effect_pr    # naive fix breaks a test -> reverted
python -m pytest code_review_agent/tests -q
```

| Scenario | Seeded problems | Result with Haiku 5.5 |
|---|---|---|
| `leaky_pr` | unused imports, unsorted imports, trailing whitespace, scaler fitted before split, no seed / stratify, training-set accuracy, prompt-injection comment | `repaired`: F401/I001/W291 fixed; leakage, evaluation, seed, split and injection reported, logic untouched |
| `side_effect_pr` | import used only for its side effect | `repaired` (I001 only): the model did not request F401 and explained the side effect. The scripted naive review requests F401 → pytest fails → `validation_failed`, reverted |

## Other backends

`LLM_BACKEND=openai` uses a tool-calling loop against any OpenAI-compatible endpoint
(`LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY`), for example an Anthropic API key or a local
Ollama server. Run the same demo scenarios against any model before switching.
