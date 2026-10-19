-- Orchestrator: one row per run (trigger), its agent steps and the proposals it waits on.
-- A run's id is the correlation_id shared by every event, action and audit row it causes.

CREATE SCHEMA IF NOT EXISTS orchestrator;

CREATE TABLE IF NOT EXISTS orchestrator.runs (
    id               UUID PRIMARY KEY,
    trigger_type     TEXT NOT NULL,          -- pull_request, build_failed, ... or 'free_text'
    trigger          JSONB NOT NULL,         -- params, source, free text as received
    route            TEXT,                   -- chosen route, NULL while untriaged
    routed_by        TEXT CHECK (routed_by IN ('rule', 'llm', 'human')),
    route_confidence FLOAT,
    status           TEXT NOT NULL DEFAULT 'received' CHECK (status IN
                     ('received', 'needs_triage', 'running', 'waiting_approval',
                      'executing', 'completed', 'failed', 'cancelled')),
    summary          TEXT,
    error            TEXT,
    requested_by     TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at      TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_runs_status ON orchestrator.runs (status, created_at);

CREATE TABLE IF NOT EXISTS orchestrator.run_steps (
    id          BIGSERIAL PRIMARY KEY,
    run_id      UUID NOT NULL REFERENCES orchestrator.runs(id),
    step_no     INT NOT NULL,
    agent       TEXT NOT NULL,
    status      TEXT NOT NULL CHECK (status IN
                ('running', 'done', 'degraded', 'failed', 'skipped', 'unavailable')),
    input       JSONB,
    output      JSONB,
    error       TEXT,
    started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    UNIQUE (run_id, step_no)
);

-- Proposals published during a run, with the guardrail's decision and what was executed.
CREATE TABLE IF NOT EXISTS orchestrator.run_actions (
    action_id        UUID PRIMARY KEY REFERENCES public.actions(id),
    run_id           UUID NOT NULL REFERENCES orchestrator.runs(id),
    step_id          BIGINT REFERENCES orchestrator.run_steps(id),
    agent            TEXT NOT NULL,
    action_type      TEXT NOT NULL,
    decision         TEXT NOT NULL DEFAULT 'pending'
                     CHECK (decision IN ('pending', 'approved', 'rejected')),
    decided_by       TEXT,
    decided_at       TIMESTAMPTZ,
    execution        TEXT NOT NULL DEFAULT 'not_run' CHECK (execution IN
                     ('not_run', 'not_needed', 'dry_run', 'done', 'failed', 'skipped')),
    execution_result JSONB,
    executed_at      TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_run_actions_run ON orchestrator.run_actions (run_id);
