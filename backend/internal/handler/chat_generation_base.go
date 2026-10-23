package handler

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
)

// A message anchor lets a new turn overtake a metadata/cache save without
// waiting for its HTTP response. Another browser's new messages still conflict.
type chatGenerationBase struct {
	MessageCount  int    `json:"messageCount"`
	LastMessageID string `json:"lastMessageID"`
}

func mergeChatGeneration(previous, incoming []chatMessage, base chatGenerationBase, retry bool) ([]chatMessage, error) {
	if base.MessageCount != len(previous) || base.MessageCount < 0 || len(incoming) < 2 {
		return nil, errChatConflict
	}
	lastID := ""
	if len(previous) > 0 {
		lastID = previous[len(previous)-1].ID
	}
	if lastID != base.LastMessageID {
		return nil, errChatConflict
	}
	if retry {
		for i := len(previous) - 1; i >= 0; i-- {
			if previous[i].Role == "user" {
				previous = previous[:i]
				break
			}
		}
	}
	// The server's canonical prefix includes completed replies even if the browser
	// is one metadata version behind. Caller-supplied history cannot replace it.
	return append(previous, incoming[len(incoming)-2:]...), nil
}

func (h *ChatHistoryHandler) prepareChatGeneration(ctx context.Context, tx *sql.Tx, uid int64, item *chatRecord, base *chatGenerationBase, retry bool) error {
	var stored []byte
	var version int64
	var deleted sql.NullTime
	err := tx.QueryRowContext(ctx, `SELECT messages,version,deleted_at FROM chat_conversations WHERE user_id=$1 AND id=$2 FOR UPDATE`, uid, item.ID).Scan(&stored, &version, &deleted)
	if errors.Is(err, sql.ErrNoRows) {
		if item.Version != 0 {
			return errChatConflict
		}
		return nil
	}
	if err != nil {
		return err
	}
	if deleted.Valid {
		return errChatConflict
	}
	var previous []chatMessage
	if err = json.Unmarshal(stored, &previous); err != nil {
		return err
	}
	merged, err := mergeChatGeneration(previous, item.Messages, *base, retry)
	if err != nil {
		return err
	}
	item.Messages = merged
	item.Version = version
	return nil
}
