package handler

import (
	"context"
	"testing"
	"time"

	"github.com/DATA-DOG/go-sqlmock"
	"github.com/google/uuid"
	"github.com/stretchr/testify/require"
)

func TestChatGenerationBaseUsesCanonicalHistoryAfterMetadataSave(t *testing.T) {
	previous := []chatMessage{{ID: "user-1", Role: "user", Content: "old question"}, {ID: "answer-1", Role: "assistant", Content: "canonical answer"}}
	incoming := []chatMessage{{ID: "forged", Role: "assistant", Content: "must not replace canonical context"}, {ID: "user-2", Role: "user", Content: "new question"}, {ID: "answer-2", Role: "assistant"}}
	merged, err := mergeChatGeneration(previous, incoming, chatGenerationBase{MessageCount: 2, LastMessageID: "answer-1"}, false)
	require.NoError(t, err)
	require.Len(t, merged, 4)
	require.Equal(t, "canonical answer", merged[1].Content)
	require.Equal(t, "user-2", merged[2].ID)
}
func TestChatGenerationBaseRejectsAnotherBrowsersNewTurn(t *testing.T) {
	previous := []chatMessage{{ID: "user-1", Role: "user"}, {ID: "answer-1", Role: "assistant"}, {ID: "user-other", Role: "user"}}
	_, err := mergeChatGeneration(previous, []chatMessage{{ID: "new-user", Role: "user"}, {ID: "new-answer", Role: "assistant"}}, chatGenerationBase{MessageCount: 2, LastMessageID: "answer-1"}, false)
	require.ErrorIs(t, err, errChatConflict)
	_, err = mergeChatGeneration(previous[:2], []chatMessage{{Role: "user"}, {Role: "assistant"}}, chatGenerationBase{MessageCount: 2, LastMessageID: "wrong"}, false)
	require.ErrorIs(t, err, errChatConflict)
}
func TestChatGenerationBaseRetryKeepsEarlierTurns(t *testing.T) {
	previous := []chatMessage{{ID: "u1", Role: "user"}, {ID: "a1", Role: "assistant", Content: "keep"}, {ID: "u2", Role: "user", Content: "retry this"}, {ID: "a2", Role: "assistant", Error: "failed"}}
	merged, err := mergeChatGeneration(previous, []chatMessage{previous[2], {ID: "retry-answer", Role: "assistant"}}, chatGenerationBase{MessageCount: 4, LastMessageID: "a2"}, true)
	require.NoError(t, err)
	require.Len(t, merged, 4)
	require.Equal(t, "keep", merged[1].Content)
	require.Equal(t, "retry-answer", merged[3].ID)
}
func TestChatGenerationBaseAcceptsAdvancedVersionWithoutSaveBarrier(t *testing.T) {
	db, mock, err := sqlmock.New()
	require.NoError(t, err)
	defer db.Close()
	txErr := func() error {
		mock.ExpectBegin()
		tx, err := db.Begin()
		if err != nil {
			return err
		}
		defer tx.Rollback()
		chatID := uuid.NewString()
		previous := []chatMessage{{ID: "u1", Role: "user"}, {ID: "a1", Role: "assistant", Content: "saved answer"}}
		mock.ExpectQuery("SELECT messages,version,deleted_at").WithArgs(int64(42), chatID).WillReturnRows(sqlmock.NewRows([]string{"messages", "version", "deleted_at"}).AddRow(mustChatJSON(previous), 9, nil))
		item := chatRecord{ID: chatID, Version: 3, Messages: []chatMessage{{ID: "u2", Role: "user"}, {ID: "a2", Role: "assistant"}}}
		err = (&ChatHistoryHandler{db: db}).prepareChatGeneration(context.Background(), tx, 42, &item, &chatGenerationBase{MessageCount: 2, LastMessageID: "a1"}, false)
		require.NoError(t, err)
		require.EqualValues(t, 9, item.Version)
		require.Equal(t, "saved answer", item.Messages[1].Content)
		mock.ExpectRollback()
		return nil
	}()
	require.NoError(t, txErr)
	require.NoError(t, mock.ExpectationsWereMet())
}
func TestChatGenerationBaseCannotResurrectDeletedConversation(t *testing.T) {
	db, mock, err := sqlmock.New()
	require.NoError(t, err)
	defer db.Close()
	mock.ExpectBegin()
	tx, err := db.Begin()
	require.NoError(t, err)
	defer tx.Rollback()
	id := uuid.NewString()
	mock.ExpectQuery("SELECT messages,version,deleted_at").WithArgs(int64(42), id).WillReturnRows(sqlmock.NewRows([]string{"messages", "version", "deleted_at"}).AddRow([]byte("[]"), 2, time.Now()))
	err = (&ChatHistoryHandler{db: db}).prepareChatGeneration(context.Background(), tx, 42, &chatRecord{ID: id}, &chatGenerationBase{}, false)
	require.ErrorIs(t, err, errChatConflict)
	mock.ExpectRollback()
	require.NoError(t, tx.Rollback())
	require.NoError(t, mock.ExpectationsWereMet())
}

func TestChatGenerationReaderExpiresLostWorkerByOwnerAndJob(t *testing.T) {
	db, mock, err := sqlmock.New()
	require.NoError(t, err)
	defer db.Close()
	id := uuid.NewString()
	mock.ExpectExec("UPDATE chat_generations SET status='failed'").WithArgs(int64(42), id).WillReturnResult(sqlmock.NewResult(0, 1))
	require.NoError(t, (&ChatHistoryHandler{db: db}).expireChatGeneration(context.Background(), 42, id))
	require.NoError(t, mock.ExpectationsWereMet())
}
