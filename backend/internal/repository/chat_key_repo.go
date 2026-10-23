package repository

import "context"

// The reserved prefix is immutable through user key CRUD. ON CONFLICT guarantees
// that simultaneous first chats share one durable credential and usage identity.
func (r *apiKeyRepository) EnsureChatKey(ctx context.Context, userID, groupID int64, candidate string) (string, error) {
	const query = `INSERT INTO api_keys (user_id, group_id, key, name, status, created_at, updated_at)
 VALUES ($1, $2, $3, '在线对话', 'active', NOW(), NOW())
 ON CONFLICT (user_id) WHERE key LIKE 'sk-internal-chat-%' AND deleted_at IS NULL
 DO UPDATE SET group_id = EXCLUDED.group_id, updated_at = NOW()
 RETURNING key`
	var key string
	err := scanSingleRow(ctx, r.sql, query, []any{userID, groupID, candidate}, &key)
	return key, err
}
