-- Guardrail agent: approval workflow + tamper-evident audit log.
-- Idempotent: safe to run more than once, on a fresh or an existing database.
-- Fresh volume: loaded automatically by docker-compose after schema.sql.
-- Existing volume / Neon:
--   docker compose exec -T postgres psql -U devops -d agents < db/migrations/002_guardrail.sql

-- ---------------------------------------------------------------- approvals
ALTER TABLE approvals
    ADD COLUMN IF NOT EXISTS risk               TEXT,
    ADD COLUMN IF NOT EXISTS policy_rule        TEXT,
    ADD COLUMN IF NOT EXISTS reason             TEXT,
    ADD COLUMN IF NOT EXISTS llm_summary        TEXT,
    ADD COLUMN IF NOT EXISTS required_approvals INT NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS requested_by       TEXT,  -- who triggered the action; cannot approve it
    ADD COLUMN IF NOT EXISTS created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    ADD COLUMN IF NOT EXISTS expires_at         TIMESTAMPTZ;

DO $$ BEGIN
    -- one approval per action: an action can never be decided twice
    ALTER TABLE approvals ADD CONSTRAINT approvals_action_unique UNIQUE (action_id);
EXCEPTION WHEN duplicate_table OR duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    ALTER TABLE approvals ADD CONSTRAINT approvals_status_check
        CHECK (status IN ('pending', 'approved', 'rejected', 'expired'));
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE INDEX IF NOT EXISTS idx_approvals_pending ON approvals (created_at) WHERE status = 'pending';

-- One row per human vote. The primary key stops one person voting twice,
-- which is what makes the two-person rule real.
CREATE TABLE IF NOT EXISTS approval_votes (
    approval_id UUID NOT NULL REFERENCES approvals(id),
    voter       TEXT NOT NULL,
    vote        TEXT NOT NULL CHECK (vote IN ('approve', 'reject')),
    voted_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (approval_id, voter)
);

-- ------------------------------------------- reasoning_logs: hash chain
-- Every row stores sha256(previous row's hash + this row's content).
-- Editing or deleting any past row breaks the chain from that point on,
-- and verify_reasoning_chain() reports where. The trigger covers every
-- writer (all agents via publish_and_log), not just the guardrail.
ALTER TABLE reasoning_logs
    ADD COLUMN IF NOT EXISTS prev_hash TEXT,
    ADD COLUMN IF NOT EXISTS row_hash  TEXT;

CREATE OR REPLACE FUNCTION reasoning_log_hash(r reasoning_logs, prev TEXT) RETURNS TEXT
LANGUAGE sql STABLE AS $$
    SELECT encode(sha256(convert_to(concat_ws('|',
        coalesce(prev, 'genesis'), r.id, r.correlation_id, r.agent_name,
        coalesce(r.input_summary, ''), coalesce(r.reasoning_trace::text, ''),
        coalesce(r.decision, ''), coalesce(r.confidence::text, ''),
        extract(epoch FROM r.created_at)::text), 'UTF8')), 'hex')
$$;

CREATE OR REPLACE FUNCTION reasoning_logs_chain() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    -- Serialise writers so each row links to the one committed before it,
    -- and take the id under the lock so chain order == id order.
    PERFORM pg_advisory_xact_lock(hashtext('reasoning_logs_chain'));
    NEW.id := nextval(pg_get_serial_sequence('reasoning_logs', 'id'));
    SELECT row_hash INTO NEW.prev_hash FROM reasoning_logs ORDER BY id DESC LIMIT 1;
    NEW.row_hash := reasoning_log_hash(NEW, NEW.prev_hash);
    RETURN NEW;
END $$;

CREATE OR REPLACE FUNCTION reasoning_logs_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'reasoning_logs is append-only (% refused)', TG_OP;
END $$;

DROP TRIGGER IF EXISTS reasoning_logs_chain ON reasoning_logs;
CREATE TRIGGER reasoning_logs_chain BEFORE INSERT ON reasoning_logs
    FOR EACH ROW EXECUTE FUNCTION reasoning_logs_chain();

DROP TRIGGER IF EXISTS reasoning_logs_append_only ON reasoning_logs;
CREATE TRIGGER reasoning_logs_append_only BEFORE UPDATE OR DELETE ON reasoning_logs
    FOR EACH ROW EXECUTE FUNCTION reasoning_logs_append_only();

-- TRUNCATE skips row triggers and would leave an empty, "intact" chain.
DROP TRIGGER IF EXISTS reasoning_logs_no_truncate ON reasoning_logs;
CREATE TRIGGER reasoning_logs_no_truncate BEFORE TRUNCATE ON reasoning_logs
    FOR EACH STATEMENT EXECUTE FUNCTION reasoning_logs_append_only();

-- Rows written before this migration have no hash: chain them once, in id order.
DO $$
DECLARE r reasoning_logs; prev TEXT := NULL;
BEGIN
    IF EXISTS (SELECT 1 FROM reasoning_logs WHERE row_hash IS NULL) THEN
        ALTER TABLE reasoning_logs DISABLE TRIGGER reasoning_logs_append_only;
        FOR r IN SELECT * FROM reasoning_logs ORDER BY id LOOP
            IF r.row_hash IS NULL THEN
                UPDATE reasoning_logs SET prev_hash = prev, row_hash = reasoning_log_hash(r, prev)
                WHERE id = r.id RETURNING row_hash INTO prev;
            ELSE
                prev := r.row_hash;
            END IF;
        END LOOP;
        ALTER TABLE reasoning_logs ENABLE TRIGGER reasoning_logs_append_only;
    END IF;
END $$;

-- Returns one row per broken link; an empty result means the log is intact.
CREATE OR REPLACE FUNCTION verify_reasoning_chain()
RETURNS TABLE (id INT, problem TEXT)
LANGUAGE sql STABLE AS $$
    WITH chain AS (
        SELECT r, lag(r.row_hash) OVER (ORDER BY r.id) AS expected_prev
        FROM reasoning_logs r
    )
    SELECT (r).id,
           CASE WHEN (r).prev_hash IS DISTINCT FROM expected_prev
                THEN 'prev_hash does not match the previous row (row deleted or reordered)'
                ELSE 'content changed after it was written' END
    FROM chain
    WHERE (r).prev_hash IS DISTINCT FROM expected_prev
       OR (r).row_hash IS DISTINCT FROM reasoning_log_hash(r, (r).prev_hash)
    ORDER BY 1
$$;
