-- Optional user OpenAI cap is enforced together with the global user cap.
-- Existing users inherit their global setting until explicitly configured.
ALTER TABLE users ADD COLUMN IF NOT EXISTS openai_concurrency INTEGER NOT NULL DEFAULT 0;
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'users_openai_concurrency_nonnegative' AND conrelid = 'users'::regclass) THEN
        ALTER TABLE users ADD CONSTRAINT users_openai_concurrency_nonnegative CHECK (openai_concurrency >= 0) NOT VALID;
    END IF;
END $$;
