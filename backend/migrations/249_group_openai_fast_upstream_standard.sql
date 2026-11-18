ALTER TABLE groups
    ADD COLUMN IF NOT EXISTS openai_fast_upstream_standard BOOLEAN NOT NULL DEFAULT FALSE;

COMMENT ON COLUMN groups.openai_fast_upstream_standard IS
    'Send Codex Fast requests to the standard upstream tier while preserving the group Fast billing configuration';
