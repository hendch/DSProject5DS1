-- App settings, alerts, and the GitHub activity the app follows.

-- Key/value settings changed from the dashboard (GitHub repo, auto-investigate...).
-- Secrets stored here (the GitHub token) are never returned by the API.
CREATE TABLE IF NOT EXISTS app_settings (
    key        TEXT PRIMARY KEY,
    value      JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Alerts shown in the app (bell, Alerts page, desktop notifications).
-- `dedupe_key` stops the same problem from raising the same alert twice.
CREATE TABLE IF NOT EXISTS alerts (
    id         BIGSERIAL PRIMARY KEY,
    severity   TEXT NOT NULL CHECK (severity IN ('critical', 'warning', 'info', 'success')),
    title      TEXT NOT NULL,
    message    TEXT NOT NULL DEFAULT '',
    source     TEXT NOT NULL,
    link       TEXT,
    dedupe_key TEXT UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    read_at    TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_alerts_unread ON alerts (created_at) WHERE read_at IS NULL;

CREATE SCHEMA IF NOT EXISTS github;

-- Latest known state of each commit / pull request in the followed repository.
CREATE TABLE IF NOT EXISTS github.items (
    repo       TEXT NOT NULL,
    kind       TEXT NOT NULL CHECK (kind IN ('commit', 'pull_request')),
    key        TEXT NOT NULL,                 -- commit sha, or pull request number
    sha        TEXT,
    branch     TEXT,
    title      TEXT,
    message    TEXT,
    author     TEXT,
    avatar_url TEXT,
    state      TEXT,                          -- pull requests: open | merged | closed
    ci         TEXT NOT NULL DEFAULT 'none' CHECK (ci IN ('success', 'failure', 'running', 'none')),
    ci_run_id  BIGINT,
    run_url    TEXT,
    html_url   TEXT,
    time       TIMESTAMPTZ,
    run_id     UUID,                          -- orchestrator run that investigated it
    data       JSONB NOT NULL DEFAULT '{}',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (repo, kind, key)
);
CREATE INDEX IF NOT EXISTS idx_github_items_time ON github.items (repo, time DESC);

-- What has already been alerted on / investigated, so a restart or a re-sync never
-- repeats it (keys like "run:123:failure", "pr:12:<sha>").
CREATE TABLE IF NOT EXISTS github.seen (
    repo    TEXT NOT NULL,
    key     TEXT NOT NULL,
    seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (repo, key)
);
