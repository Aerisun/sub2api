package handler

import (
	"context"
	"database/sql"
	"fmt"
	"net/http"
	"regexp"
	"testing"

	"github.com/DATA-DOG/go-sqlmock"
	"github.com/gin-gonic/gin"
	"github.com/google/uuid"
	"github.com/stretchr/testify/require"
)

func TestChatStopPersistsOrphanedCheckpointAndCancelsOwnedWorker(t *testing.T) {
	for _, live := range []bool{false, true} {
		t.Run(fmt.Sprintf("live=%t", live), func(t *testing.T) {
			db, mock, err := sqlmock.New()
			require.NoError(t, err)
			defer db.Close()
			job, chat := uuid.NewString(), uuid.NewString()
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			h := &ChatHistoryHandler{db: db, tasks: make(map[string]context.CancelFunc)}
			if live {
				h.tasks["42:"+job] = cancel
			}
			c, w := historyTestContext("POST", "/generations/"+job+"/stop", "")
			c.Params = gin.Params{{Key: "id", Value: job}}
			mock.ExpectBegin()
			mock.ExpectExec(regexp.QuoteMeta(`SELECT pg_advisory_xact_lock($1)`)).WithArgs(int64(42)).WillReturnResult(sqlmock.NewResult(0, 1))
			mock.ExpectQuery("SELECT conversation_id::text,content,reasoning,tools,status").WithArgs(int64(42), job).WillReturnRows(sqlmock.NewRows([]string{"conversation_id", "content", "reasoning", "tools", "status"}).AddRow(chat, "部分回答", "已有思考", "[]", "running"))
			mock.ExpectExec("UPDATE chat_conversations SET messages=").WithArgs(int64(42), chat, job, "部分回答", "已有思考", "[]").WillReturnResult(sqlmock.NewResult(0, 1))
			mock.ExpectExec("UPDATE chat_generations SET status='stopped'").WithArgs(int64(42), job).WillReturnResult(sqlmock.NewResult(0, 1))
			mock.ExpectCommit()
			h.StopGeneration(c)
			require.Equal(t, http.StatusOK, w.Code)
			require.Contains(t, w.Body.String(), `"stopped":true`)
			require.Equal(t, live, ctx.Err() != nil)
			require.NoError(t, mock.ExpectationsWereMet())
		})
	}
}

func TestChatStopIsAccountScopedAndIdempotent(t *testing.T) {
	for _, status := range []string{"missing", "completed", "stopped"} {
		t.Run(status, func(t *testing.T) {
			db, mock, err := sqlmock.New()
			require.NoError(t, err)
			defer db.Close()
			job := uuid.NewString()
			mock.ExpectBegin()
			mock.ExpectExec("SELECT pg_advisory_xact_lock").WithArgs(int64(42)).WillReturnResult(sqlmock.NewResult(0, 1))
			q := mock.ExpectQuery("SELECT conversation_id::text,content,reasoning,tools,status").WithArgs(int64(42), job)
			if status == "missing" {
				q.WillReturnError(sql.ErrNoRows)
			} else {
				q.WillReturnRows(sqlmock.NewRows([]string{"conversation_id", "content", "reasoning", "tools", "status"}).AddRow(uuid.NewString(), "", "", "[]", status))
			}
			mock.ExpectRollback()
			stopped, err := (&ChatHistoryHandler{db: db}).stopChatGeneration(context.Background(), 42, job)
			require.NoError(t, err)
			require.False(t, stopped)
			require.NoError(t, mock.ExpectationsWereMet())
		})
	}
}

func TestChatWorkerCannotRestartStoppedGeneration(t *testing.T) {
	db, mock, err := sqlmock.New()
	require.NoError(t, err)
	defer db.Close()
	job := uuid.NewString()
	mock.ExpectQuery("SELECT EXISTS").WithArgs(int64(42), job).WillReturnRows(sqlmock.NewRows([]string{"exists"}).AddRow(false))
	ctx, cancel := context.WithCancel(context.Background())
	h := &ChatHistoryHandler{db: db, tasks: map[string]context.CancelFunc{"42:" + job: cancel}}
	h.runGeneration(ctx, cancel, 42, chatRecord{}, job, "", nil)
	require.ErrorIs(t, ctx.Err(), context.Canceled)
	require.Empty(t, h.tasks)
	require.NoError(t, mock.ExpectationsWereMet())
}
