-- One server-only chat credential per user; normal key schema stays compatible.
CREATE UNIQUE INDEX IF NOT EXISTS api_keys_internal_chat_user_unique
ON api_keys (user_id)
WHERE key LIKE 'sk-internal-chat-%' AND deleted_at IS NULL;
