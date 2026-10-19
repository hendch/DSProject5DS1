CREATE TABLE IF NOT EXISTS agents (
    id SERIAL PRIMARY KEY,
    name TEXT UNIQUE NOT NULL
);

CREATE TABLE IF NOT EXISTS actions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    correlation_id UUID NOT NULL,
    agent_name TEXT NOT NULL,
    action_type TEXT NOT NULL,
    environment TEXT NOT NULL,
    blast_radius TEXT NOT NULL,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS approvals (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    action_id UUID REFERENCES actions(id),
    status TEXT NOT NULL DEFAULT 'pending', -- pending | approved | rejected
    decided_by TEXT,
    decided_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS reasoning_logs (
    id SERIAL PRIMARY KEY,
    correlation_id UUID NOT NULL,
    agent_name TEXT NOT NULL,
    input_summary TEXT,
    reasoning_trace JSONB,
    decision TEXT,
    confidence FLOAT,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_reasoning_logs_correlation ON reasoning_logs (correlation_id, created_at);