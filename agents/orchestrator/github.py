"""Follow a GitHub repository: commits, CI (GitHub Actions) results and pull requests.

The app polls the GitHub REST API (webhooks can't reach a laptop). On each sync it
updates github.items, raises alerts (CI failed, CI green again, PR opened/updated) and,
if auto-investigate is on, starts orchestrator runs: RCA on the real failed CI log,
the code reviewer on pull requests. github.seen makes every alert and investigation
happen once, even across restarts; connecting marks existing history as seen so it
doesn't flood the alerts.

Token: GITHUB_TOKEN in .env, or pasted in Settings (stored in app_settings, never
returned by the API). Needs read access to Contents, Actions, Checks and Pull requests.
"""
import os
import re
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import psycopg2.extras

from shared.alerts import delete_setting, get_setting, raise_alert, set_setting
from shared.db import get_connection
from shared.redact import redact, tail

API = "https://api.github.com"
RUNNING = {"queued", "in_progress", "waiting", "requested", "pending"}
FAILED = {"failure", "timed_out", "startup_failure"}

_state = {"last_sync": None, "error": None, "rate_limit": None, "syncing": False}
_etags: dict[str, tuple[str, object]] = {}
_lock = threading.Lock()


class GitHubError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


# ---- settings ----------------------------------------------------------------------------
def token() -> str | None:
    return os.getenv("GITHUB_TOKEN") or get_setting("github_token")


def config() -> dict | None:
    return get_setting("github")


def poll_seconds() -> int:
    return int((config() or {}).get("poll_seconds") or os.getenv("GITHUB_POLL_SECONDS", "60"))


# ---- HTTP --------------------------------------------------------------------------------
def _get(path: str, tok: str | None = None, params: dict | None = None, raw: bool = False):
    """GET with conditional requests (ETag: a 304 doesn't count against the rate limit)."""
    tok = tok or token()
    if not tok:
        raise GitHubError(401, "GitHub is not connected")
    url = path if path.startswith("http") else f"{API}{path}"
    key = url + str(sorted((params or {}).items()))
    headers = {"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28"}
    if not raw and key in _etags:
        headers["If-None-Match"] = _etags[key][0]
    try:
        r = httpx.get(url, headers=headers, params=params, timeout=30, follow_redirects=True)
    except httpx.HTTPError as e:
        raise GitHubError(0, f"can't reach GitHub ({type(e).__name__})") from None
    if "x-ratelimit-remaining" in r.headers:
        _state["rate_limit"] = {"remaining": int(r.headers["x-ratelimit-remaining"]),
                                "reset_at": datetime.fromtimestamp(int(r.headers.get("x-ratelimit-reset", 0)),
                                                                   timezone.utc).isoformat()}
    if r.status_code == 304 and key in _etags:
        return _etags[key][1]
    if r.status_code == 401:
        raise GitHubError(401, "GitHub rejected the token (expired or revoked)")
    if r.status_code == 403 and r.headers.get("x-ratelimit-remaining") == "0":
        raise GitHubError(429, f"GitHub rate limit reached; resets at {_state['rate_limit']['reset_at']}")
    if r.status_code == 403:
        raise GitHubError(403, "the token lacks a permission this needs (Contents, Actions, Checks, Pull requests)")
    if r.status_code == 404:
        raise GitHubError(404, "not found, or the token can't see it")
    if r.status_code >= 400:
        raise GitHubError(r.status_code, f"GitHub answered {r.status_code}: {r.text[:200]}")
    if raw:
        return r.text
    data = r.json()
    if r.headers.get("etag"):
        _etags[key] = (r.headers["etag"], data)
    return data


# ---- connect / status ----------------------------------------------------------------------
def _clean_repo(repo: str) -> str:
    repo = (repo or "").strip().strip("/").removeprefix("https://github.com/").removesuffix(".git")
    if repo.count("/") != 1 or not all(repo.split("/")):
        raise GitHubError(400, "repository must look like owner/name")
    return repo


def _check_repo(repo: str, tok: str) -> dict:
    info = _get(f"/repos/{repo}", tok)
    _get(f"/repos/{info['full_name']}/actions/runs", tok, {"per_page": 1})  # proves Actions read access
    return {"default_branch": info.get("default_branch", "main"), "private": info.get("private"),
            "full_name": info["full_name"]}


def repos() -> dict[str, dict]:
    """Followed repositories: {"owner/name": {"default_branch", "private"}}."""
    return (config() or {}).get("repos") or {}


def connect(repo: str, tok: str) -> dict:
    """Connect GitHub with a token and follow a first repository."""
    repo, tok = _clean_repo(repo), (tok or "").strip()
    if not tok:
        raise GitHubError(400, "paste a token")
    user = _get("/user", tok)
    info = _check_repo(repo, tok)
    cfg = config() or {}
    set_setting("github_token", tok)
    set_setting("github", {"login": user["login"], "avatar_url": user.get("avatar_url"),
                           "repos": {**(cfg.get("repos") or {}), info.pop("full_name"): info},
                           "auto_investigate": cfg.get("auto_investigate", True),
                           "poll_seconds": cfg.get("poll_seconds", 60)})
    _state["error"] = None
    sync(baseline=True)
    raise_alert("success", f"GitHub connected: {repo}", f"Signed in as {user['login']}.", "github",
                "#/activity", None)
    return status()


def add_repo(repo: str) -> dict:
    """Follow another repository with the same token. Its history is recorded silently."""
    cfg = config()
    if not cfg or not token():
        raise GitHubError(400, "connect GitHub first (Settings)")
    info = _check_repo(_clean_repo(repo), token())
    name = info.pop("full_name")
    if name in repos():
        raise GitHubError(409, f"{name} is already followed")
    set_setting("github", {**cfg, "repos": {**repos(), name: info}})
    sync(baseline=True, only=name)
    return repo_summary(name)


def remove_repo(repo: str) -> None:
    cfg = config()
    if not cfg or repo not in repos():
        raise GitHubError(404, f"{repo} is not followed")
    set_setting("github", {**cfg, "repos": {k: v for k, v in repos().items() if k != repo}})
    _db("DELETE FROM github.items WHERE repo = %s", (repo,))


def disconnect() -> None:
    delete_setting("github_token")
    delete_setting("github")
    _etags.clear()
    _state.update(error=None, last_sync=None)


def update_settings(**changes) -> dict:
    cfg = config()
    if not cfg:
        raise GitHubError(400, "GitHub is not connected")
    set_setting("github", {**cfg, **{k: v for k, v in changes.items() if v is not None}})
    return status()


def status() -> dict:
    cfg = config() or {}
    names = list(repos())
    return {"connected": bool(cfg and token() and names), "signed_in": bool(cfg.get("login") and token()),
            "repo": names[0] if names else None, "repos": names,
            "login": cfg.get("login"), "avatar_url": cfg.get("avatar_url"), "auth": cfg.get("auth"),
            "last_sync": _state["last_sync"], "rate_limit": _state["rate_limit"], "error": _state["error"],
            "poll_seconds": cfg.get("poll_seconds", 60), "auto_investigate": cfg.get("auto_investigate", True),
            "token_from_env": bool(os.getenv("GITHUB_TOKEN")), "oauth_ready": bool(client_id())}


# ---- "Sign in with GitHub" (OAuth device flow) ---------------------------------------------------
# The user opens github.com/login/device, types the code shown by the app and authorises it;
# meanwhile the app polls GitHub for the token. Needs the Client ID of a GitHub OAuth App with
# "Device flow" enabled (GITHUB_CLIENT_ID in .env, or pasted once in Settings; it isn't secret).
DEVICE_URL = "https://github.com/login/device/code"
TOKEN_URL = "https://github.com/login/oauth/access_token"
OAUTH_SCOPES = "repo read:user"  # OAuth apps have no read-only scope for private repositories
_device: dict = {}


def client_id() -> str | None:
    return os.getenv("GITHUB_CLIENT_ID") or get_setting("github_client_id")


def set_client_id(value: str) -> None:
    value = (value or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]{10,60}", value):
        raise GitHubError(400, "that doesn't look like a GitHub OAuth App Client ID")
    set_setting("github_client_id", value)


def _oauth_post(url: str, data: dict) -> dict:
    try:
        r = httpx.post(url, data=data, headers={"Accept": "application/json"}, timeout=20)
    except httpx.HTTPError as e:
        raise GitHubError(0, f"can't reach GitHub ({type(e).__name__})") from None
    if r.status_code >= 400:
        raise GitHubError(r.status_code, f"GitHub answered {r.status_code}: {r.text[:200]}")
    return r.json()


def device_start() -> dict:
    cid = client_id()
    if not cid:
        raise GitHubError(400, "add your GitHub OAuth App's Client ID first (Settings)")
    data = _oauth_post(DEVICE_URL, {"client_id": cid, "scope": OAUTH_SCOPES})
    if "device_code" not in data:
        raise GitHubError(400, data.get("error_description") or "GitHub refused to start the sign-in "
                          "(is Device flow enabled on the OAuth App?)")
    _device.clear()
    _device.update(code=data["device_code"], interval=int(data.get("interval", 5)),
                   expires=time.monotonic() + int(data.get("expires_in", 900)), next_poll=0.0)
    return {"user_code": data["user_code"], "verification_uri": data["verification_uri"],
            "expires_in": data.get("expires_in", 900), "interval": _device["interval"]}


def device_poll() -> dict:
    """Ask GitHub whether the user has authorised yet. Safe to call often: it respects
    GitHub's interval and only calls GitHub when allowed."""
    if not _device:
        return {"state": "idle"}
    if time.monotonic() > _device["expires"]:
        _device.clear()
        return {"state": "expired"}
    if time.monotonic() < _device["next_poll"]:
        return {"state": "pending"}
    _device["next_poll"] = time.monotonic() + _device["interval"]
    data = _oauth_post(TOKEN_URL, {"client_id": client_id(), "device_code": _device["code"],
                                   "grant_type": "urn:ietf:params:oauth:grant-type:device_code"})
    error = data.get("error")
    if error == "authorization_pending":
        return {"state": "pending"}
    if error == "slow_down":
        _device["interval"] = int(data.get("interval", _device["interval"] + 5))
        return {"state": "pending"}
    if error in ("expired_token", "access_denied"):
        _device.clear()
        return {"state": "expired" if error == "expired_token" else "denied"}
    if "access_token" not in data:
        _device.clear()
        raise GitHubError(400, data.get("error_description") or f"sign-in failed ({error})")
    _device.clear()
    sign_in(data["access_token"], auth="oauth")
    return {"state": "connected", **status()}


def sign_in(tok: str, auth: str = "token") -> dict:
    """Store a token and who it belongs to. Repositories are added afterwards."""
    user = _get("/user", tok)
    cfg = config() or {}
    set_setting("github_token", tok)
    set_setting("github", {**cfg, "login": user["login"], "avatar_url": user.get("avatar_url"), "auth": auth,
                           "repos": cfg.get("repos") or {},
                           "auto_investigate": cfg.get("auto_investigate", True),
                           "poll_seconds": cfg.get("poll_seconds", 60)})
    _state["error"] = None
    raise_alert("success", f"Signed in to GitHub as {user['login']}", "Choose the repositories to follow.",
                "github", "#/repositories", None)
    return status()


def available_repos() -> list[dict]:
    """Repositories the signed-in user can see, most recently pushed first, for the picker."""
    followed = set(repos())
    items = _get("/user/repos", params={"per_page": 100, "sort": "pushed", "affiliation": "owner,collaborator,organization_member"})
    return [{"repo": r["full_name"], "private": r["private"], "default_branch": r.get("default_branch"),
             "pushed_at": r.get("pushed_at"), "description": r.get("description"),
             "followed": r["full_name"] in followed} for r in items]


def repo_summary(repo: str) -> dict:
    info = repos().get(repo, {})
    return {"repo": repo, "default_branch": info.get("default_branch"), "private": info.get("private"),
            "head": head(repo), **counts(repo), "last_sync": _state["last_sync"]}


# ---- storage ------------------------------------------------------------------------------
def _db(sql, params=(), fetch=False):
    conn = get_connection()
    try:
        with conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()] if fetch else None
    finally:
        conn.close()


def _seen(repo: str, key: str) -> bool:
    """Mark `key` as handled. True if it already was."""
    conn = get_connection()
    try:
        with conn, conn.cursor() as cur:
            cur.execute("INSERT INTO github.seen (repo, key) VALUES (%s, %s) ON CONFLICT DO NOTHING RETURNING key",
                        (repo, key))
            return cur.fetchone() is None
    finally:
        conn.close()


def _upsert(repo: str, item: dict) -> None:
    _db("""INSERT INTO github.items (repo, kind, key, sha, branch, title, message, author, avatar_url, state,
                                     ci, ci_run_id, run_url, html_url, time, data, updated_at)
           VALUES (%(repo)s, %(kind)s, %(key)s, %(sha)s, %(branch)s, %(title)s, %(message)s, %(author)s,
                   %(avatar_url)s, %(state)s, %(ci)s, %(ci_run_id)s, %(run_url)s, %(html_url)s, %(time)s,
                   '{}', now())
           ON CONFLICT (repo, kind, key) DO UPDATE SET sha = EXCLUDED.sha, branch = coalesce(EXCLUDED.branch,
             github.items.branch), title = EXCLUDED.title, message = EXCLUDED.message, author = EXCLUDED.author,
             avatar_url = EXCLUDED.avatar_url, state = EXCLUDED.state, ci = EXCLUDED.ci,
             ci_run_id = EXCLUDED.ci_run_id, run_url = EXCLUDED.run_url, html_url = EXCLUDED.html_url,
             time = EXCLUDED.time, updated_at = now(),
             -- a pull request with new commits needs a new review
             run_id = CASE WHEN github.items.sha IS DISTINCT FROM EXCLUDED.sha THEN NULL
                           ELSE github.items.run_id END""",
        {"repo": repo, "state": None, "ci_run_id": None, "run_url": None, "avatar_url": None, **item})


def _repos_filter(repo: str | None) -> list[str]:
    names = list(repos())
    return [repo] if repo in names else names


def list_items(kind="all", branch=None, failing=False, limit=60, repo=None) -> list[dict]:
    names = _repos_filter(repo)
    if not names:
        return []
    where, params = ["repo = ANY(%s)"], [names]
    if kind in ("commit", "pull_request"):
        where.append("kind = %s")
        params.append(kind)
    if branch:
        where.append("branch = %s")
        params.append(branch)
    if failing:
        where.append("ci = 'failure'")
    return _db(f"SELECT * FROM github.items WHERE {' AND '.join(where)} ORDER BY time DESC NULLS LAST LIMIT %s",
               (*params, limit), fetch=True)


def get_item(kind: str, key: str, repo: str | None = None) -> dict | None:
    rows = _db("""SELECT * FROM github.items WHERE repo = ANY(%s) AND kind = %s AND key = %s
                  ORDER BY updated_at DESC LIMIT 1""", (_repos_filter(repo), kind, str(key)), fetch=True)
    return rows[0] if rows else None


def set_item_run(repo: str, kind: str, key: str, run_id: str) -> None:
    _db("UPDATE github.items SET run_id = %s WHERE repo = %s AND kind = %s AND key = %s",
        (run_id, repo, kind, str(key)))


def branches(repo: str | None = None) -> list[str]:
    names = _repos_filter(repo)
    return [r["branch"] for r in _db("""SELECT DISTINCT branch FROM github.items
                                        WHERE repo = ANY(%s) AND branch IS NOT NULL ORDER BY branch""",
                                     (names,), fetch=True)] if names else []


def head(repo: str | None = None) -> dict | None:
    """Latest commit on the default branch of `repo` (default: the first followed repository)."""
    names = _repos_filter(repo)
    if not names:
        return None
    rows = _db("""SELECT * FROM github.items WHERE repo = %s AND kind = 'commit' AND branch = %s
                  ORDER BY time DESC NULLS LAST LIMIT 1""",
               (names[0], repos()[names[0]].get("default_branch")), fetch=True)
    return rows[0] if rows else None


def counts(repo: str | None = None) -> dict:
    names = _repos_filter(repo)
    if not names:
        return {"open_prs": 0, "failing_today": 0}
    row = _db("""SELECT count(*) FILTER (WHERE kind = 'pull_request' AND state = 'open') AS open_prs,
                        count(*) FILTER (WHERE ci = 'failure' AND time > now() - interval '1 day') AS failing_today
                 FROM github.items WHERE repo = ANY(%s)""", (names,), fetch=True)[0]
    return {k: int(v) for k, v in row.items()}


# ---- sync ---------------------------------------------------------------------------------
def _ci(run: dict | None) -> str:
    if not run:
        return "none"
    if run.get("status") in RUNNING:
        return "running"
    if run.get("conclusion") == "success":
        return "success"
    if run.get("conclusion") in FAILED:
        return "failure"
    return "none"


def sync(baseline: bool = False, orchestrator=None, only: str | None = None) -> dict:
    """Fetch the latest commits, CI runs and pull requests of every followed repository
    (or `only` one). `baseline`: record without alerting or investigating."""
    cfg = config()
    if not cfg or not token():
        return status()
    if not _lock.acquire(blocking=False):
        return status()  # a sync is already running
    errors = []
    try:
        for repo, info in repos().items():
            if only and repo != only:
                continue
            try:
                _sync(repo, info, baseline, orchestrator, cfg.get("auto_investigate", True))
            except GitHubError as e:
                errors.append(f"{repo}: {e}")
                if e.status == 401:
                    raise_alert("critical", "GitHub token rejected", str(e) + " Reconnect it in Settings.",
                                "github", "#/settings", f"github-401:{datetime.now():%Y%m%d}")
                    break
                if e.status == 429:
                    raise_alert("warning", "GitHub rate limit reached", str(e), "github", "#/settings",
                                f"github-429:{datetime.now():%Y%m%d%H}")
                    break
                if e.status == 404:
                    raise_alert("warning", f"Can't read {repo}", str(e), "github", "#/repositories",
                                f"github-404:{repo}:{datetime.now():%Y%m%d}")
        _state.update(last_sync=datetime.now(timezone.utc).isoformat(), error="; ".join(errors) or None)
    finally:
        _lock.release()
    return status()


def _sync(repo: str, info: dict, baseline: bool, orchestrator, auto: bool) -> None:
    default = info.get("default_branch", "main")
    runs = _get(f"/repos/{repo}/actions/runs", params={"per_page": 40}).get("workflow_runs", [])
    latest_run: dict[str, dict] = {}
    for run in sorted(runs, key=lambda r: r.get("created_at") or ""):
        latest_run[run["head_sha"]] = run  # keep the newest run per commit
    commits = _get(f"/repos/{repo}/commits", params={"sha": default, "per_page": 25})
    seen_shas = set()
    for c in commits:
        run = latest_run.get(c["sha"])
        msg = c["commit"]["message"]
        _upsert(repo, {"kind": "commit", "key": c["sha"], "sha": c["sha"], "branch": default,
                       "title": msg.splitlines()[0][:200], "message": redact(msg)[:2000],
                       "author": (c.get("author") or {}).get("login") or c["commit"]["author"]["name"],
                       "avatar_url": (c.get("author") or {}).get("avatar_url"), "ci": _ci(run),
                       "ci_run_id": run["id"] if run else None, "run_url": run["html_url"] if run else None,
                       "html_url": c["html_url"], "time": c["commit"]["author"]["date"]})
        seen_shas.add(c["sha"])
    for sha, run in latest_run.items():  # commits on other branches, known from their CI runs
        if sha in seen_shas:
            continue
        hc = run.get("head_commit") or {}
        msg = hc.get("message") or run.get("display_title") or ""
        _upsert(repo, {"kind": "commit", "key": sha, "sha": sha, "branch": run.get("head_branch"),
                       "title": msg.splitlines()[0][:200] if msg else sha[:7], "message": redact(msg)[:2000],
                       "author": (run.get("actor") or {}).get("login") or (hc.get("author") or {}).get("name"),
                       "avatar_url": (run.get("actor") or {}).get("avatar_url"), "ci": _ci(run),
                       "ci_run_id": run["id"], "run_url": run["html_url"],
                       "html_url": f"https://github.com/{repo}/commit/{sha}", "time": hc.get("timestamp") or run.get("created_at")})
    pulls = _get(f"/repos/{repo}/pulls", params={"state": "all", "per_page": 20, "sort": "updated", "direction": "desc"})
    for p in pulls:
        run = latest_run.get(p["head"]["sha"])
        state = "merged" if p.get("merged_at") else p["state"]
        _upsert(repo, {"kind": "pull_request", "key": str(p["number"]), "sha": p["head"]["sha"],
                       "branch": p["head"]["ref"], "title": f"#{p['number']} {p['title']}"[:200],
                       "message": redact(p.get("body") or "")[:2000], "author": p["user"]["login"],
                       "avatar_url": p["user"].get("avatar_url"), "state": state, "ci": _ci(run),
                       "ci_run_id": run["id"] if run else None, "run_url": run["html_url"] if run else None,
                       "html_url": p["html_url"], "time": p["updated_at"]})
    _react(repo, runs, pulls, baseline, auto, orchestrator)


def _react(repo, runs, pulls, baseline, auto, orchestrator) -> None:
    """Alerts and automatic investigations for what changed since the last sync."""
    last_by_branch: dict[str, str] = {}
    for run in sorted(runs, key=lambda r: r.get("created_at") or ""):
        ci, branch = _ci(run), run.get("head_branch") or "?"
        title = ((run.get("head_commit") or {}).get("message") or run.get("display_title") or "").splitlines()[0][:120] if (
            (run.get("head_commit") or {}).get("message") or run.get("display_title")) else run["head_sha"][:7]
        if ci == "failure" and not _seen(repo, f"run:{run['id']}:failure") and not baseline:
            raise_alert("critical", f"CI failed on {branch}", f"{run['name']} · {title}", "github",
                        run["html_url"], f"ci-failed:{run['id']}")
            if auto and orchestrator:
                try:
                    investigate("commit", run["head_sha"], orchestrator, requested_by="github", repo=repo)
                except Exception as e:  # noqa: BLE001
                    print(f"[github] auto-investigation failed: {e}")
        elif ci == "success" and last_by_branch.get(branch) == "failure" \
                and not _seen(repo, f"run:{run['id']}:fixed") and not baseline:
            raise_alert("success", f"CI is green again on {branch}", f"{run['name']} · {title}", "github",
                        run["html_url"], f"ci-fixed:{run['id']}")
        if ci in ("success", "failure"):
            last_by_branch[branch] = ci
        if baseline:  # history: remember it so later syncs never alert on it
            _seen(repo, f"run:{run['id']}:{ci}")
            if ci == "success":
                _seen(repo, f"run:{run['id']}:fixed")
    for p in pulls:
        key = f"pr:{p['number']}:{p['head']['sha']}"
        if p["state"] != "open":
            continue
        if _seen(repo, key) or baseline:
            continue
        raise_alert("info", f"Pull request #{p['number']} {'opened' if p['created_at'] == p['updated_at'] else 'updated'}",
                    p["title"], "github", p["html_url"], f"pr:{p['number']}:{p['head']['sha']}")
        if auto and orchestrator:
            try:
                investigate("pull_request", p["number"], orchestrator, requested_by="github", repo=repo)
            except Exception as e:  # noqa: BLE001
                print(f"[github] auto-review failed: {e}")


# ---- investigations -------------------------------------------------------------------------
def failed_log(repo: str, run_id: int) -> str:
    """The failing jobs' logs of a workflow run, secrets removed, cut to the end."""
    jobs = _get(f"/repos/{repo}/actions/runs/{run_id}/jobs", params={"per_page": 50}).get("jobs", [])
    failing = [j for j in jobs if j.get("conclusion") in FAILED] or jobs
    parts = []
    for job in failing[:3]:
        try:
            text = _get(f"/repos/{repo}/actions/jobs/{job['id']}/logs", raw=True)
        except GitHubError as e:  # logs expired or not readable: fall back to the step list
            steps = [f"{s['name']}: {s.get('conclusion')}" for s in job.get("steps", [])]
            text = f"(log unavailable: {e})\n" + "\n".join(steps)
        parts.append(f"=== job: {job['name']} ({job.get('conclusion')}) ===\n{tail(text, 250)}")
    return redact("\n\n".join(parts)) or "(no job logs found)"


def investigate(kind: str, key, orchestrator, requested_by: str | None = None, repo: str | None = None) -> str:
    """Start an orchestrator run for a failed commit or a pull request. Returns its run id."""
    if not config() or not repos():
        raise GitHubError(400, "GitHub is not connected")
    item = get_item(kind, str(key), repo)
    if item is None:
        raise GitHubError(404, f"no {kind.replace('_', ' ')} {key} in the activity feed; try Sync now")
    repo = item["repo"]
    if item.get("run_id"):
        return str(item["run_id"])  # already investigated: don't start a duplicate
    if kind == "commit":
        if item["ci"] != "failure" or not item.get("ci_run_id"):
            raise GitHubError(409, "this commit's CI didn't fail: nothing to investigate")
        trigger = {"title": f"CI failed: {item['title']}", "source": "github", "route": "build_failed",
                   "sha": item["sha"], "html_url": item.get("run_url") or item.get("html_url"),
                   "params": {"log_text": failed_log(repo, item["ci_run_id"]), "environment": "dev",
                              "sha": item["sha"], "branch": item.get("branch")}}
    else:
        pr = _get(f"/repos/{repo}/pulls/{key}")
        trigger = {"title": f"Review PR #{key}: {pr['title']}", "source": "github", "route": "pull_request",
                   "pr": int(key), "sha": pr["head"]["sha"], "html_url": pr["html_url"],
                   "params": {"source": "github", "repo": repo, "pr": int(key), "head_sha": pr["head"]["sha"],
                              "base_sha": pr["base"]["sha"], "base_ref": pr["base"]["ref"],
                              "head_ref": pr["head"]["ref"]}}
    run_id = orchestrator.start(trigger, requested_by=requested_by)
    set_item_run(repo, kind, str(key), run_id)
    return run_id


def checkout_pull_request(params: dict, workdir: Path) -> tuple[Path, str, str]:
    """Shallow, read-only clone of the PR for the code reviewer. The token goes in a
    one-off HTTP header, never into the clone's config."""
    dest = Path(workdir) / "pr"
    url = f"https://github.com/{params['repo']}.git"
    auth = ["-c", f"http.extraHeader=Authorization: Bearer {token()}"] if token() else []
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}

    def git(*args, cwd=None):
        subprocess.run(["git", *auth, *args], cwd=cwd, check=True, capture_output=True, text=True,
                       timeout=300, env=env)

    git("clone", "--filter=blob:none", "--no-checkout", url, str(dest))
    git("fetch", "origin", params["base_sha"], f"pull/{params['pr']}/head", cwd=dest)
    git("checkout", "-q", params["head_sha"], cwd=dest)
    return dest, params["base_sha"], params["head_sha"]


# ---- background polling -----------------------------------------------------------------------
def poll_forever(orchestrator, stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            if config() and token():
                sync(orchestrator=orchestrator)
        except Exception as e:  # noqa: BLE001 - never let the poller die
            _state["error"] = f"{type(e).__name__}: {e}"
        stop.wait(max(20, poll_seconds()))
