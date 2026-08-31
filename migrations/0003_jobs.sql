-- Durable, leased background jobs for request-free parsing and crash-safe retry.
-- Claims use SELECT ... FOR UPDATE SKIP LOCKED and an expiring lease.

CREATE TABLE IF NOT EXISTS jobs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id UUID NOT NULL REFERENCES documents(id) ON DELETE RESTRICT,
    job_type TEXT NOT NULL CHECK (btrim(job_type) <> ''),
    payload JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(payload) = 'object'),
    idempotency_key TEXT NOT NULL CHECK (btrim(idempotency_key) <> ''),
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'running', 'done', 'failed', 'dead')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    last_error TEXT,
    available_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    lease_expires_at TIMESTAMPTZ,
    lease_owner TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (document_id, idempotency_key)
);

-- Claim with: WHERE status = 'queued' AND available_at <= now()
-- ORDER BY available_at, created_at FOR UPDATE SKIP LOCKED.
CREATE INDEX IF NOT EXISTS jobs_claim_queued_idx
    ON jobs (available_at, created_at, id)
    WHERE status = 'queued';

CREATE INDEX IF NOT EXISTS jobs_document_created_idx
    ON jobs (document_id, created_at DESC);

CREATE INDEX IF NOT EXISTS jobs_expired_lease_idx
    ON jobs (lease_expires_at)
    WHERE status = 'running';

CREATE OR REPLACE FUNCTION jobs_touch_updated_at()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS jobs_touch_updated_at ON jobs;
CREATE TRIGGER jobs_touch_updated_at
    BEFORE UPDATE ON jobs
    FOR EACH ROW
    EXECUTE FUNCTION jobs_touch_updated_at();
