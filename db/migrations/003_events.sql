-- Event bus in PostgreSQL (replaces Redis Streams).
--
-- `events` is an append-only log: one row per event, `body` holds the event exactly as
-- agents produce it {event_id, correlation_id, agent_name, action_type, environment,
-- blast_radius, payload, timestamp}. Agents publish in the same transaction as their
-- `actions` / `reasoning_logs` rows (transactional outbox), so an event can never exist
-- without its action, or the other way round.
--
-- Each consumer (guardrail, orchestrator...) records what it handled in
-- `event_receipts`: done, retry later, or dead after too many failures.
-- Inserting an event sends NOTIFY on channel 'events', so consumers wake up at once.

CREATE TABLE IF NOT EXISTS events (
    id         BIGSERIAL PRIMARY KEY,
    body       JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_events_event_id ON events ((body->>'event_id'));
CREATE INDEX IF NOT EXISTS idx_events_correlation ON events ((body->>'correlation_id'), id);

CREATE TABLE IF NOT EXISTS event_receipts (
    consumer        TEXT NOT NULL,
    event_id        BIGINT NOT NULL REFERENCES events(id),
    status          TEXT NOT NULL CHECK (status IN ('done', 'retry', 'dead')),
    attempts        INT NOT NULL DEFAULT 1,
    last_error      TEXT,
    next_attempt_at TIMESTAMPTZ,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (consumer, event_id)
);
CREATE INDEX IF NOT EXISTS idx_event_receipts_retry
    ON event_receipts (consumer, next_attempt_at) WHERE status = 'retry';

CREATE OR REPLACE FUNCTION events_notify() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    PERFORM pg_notify('events', NEW.id::text);
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS events_notify ON events;
CREATE TRIGGER events_notify AFTER INSERT ON events
    FOR EACH ROW EXECUTE FUNCTION events_notify();

-- The log is append-only, like reasoning_logs.
CREATE OR REPLACE FUNCTION append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% is append-only (% refused)', TG_TABLE_NAME, TG_OP;
END $$;

DROP TRIGGER IF EXISTS events_append_only ON events;
CREATE TRIGGER events_append_only BEFORE UPDATE OR DELETE ON events
    FOR EACH ROW EXECUTE FUNCTION append_only();
DROP TRIGGER IF EXISTS events_no_truncate ON events;
CREATE TRIGGER events_no_truncate BEFORE TRUNCATE ON events
    FOR EACH STATEMENT EXECUTE FUNCTION append_only();
