package handler

import (
	"bufio"
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/DATA-DOG/go-sqlmock"
	"github.com/Wei-Shaw/sub2api/internal/server/middleware"
	"github.com/Wei-Shaw/sub2api/internal/service"
	"github.com/gin-gonic/gin"
	"github.com/google/uuid"
	"github.com/stretchr/testify/require"
)

func TestChatLiveUnicodeResumeAndAppend(t *testing.T) {
	for offset, want := range []string{"你🌟好", "🌟好", "好", ""} {
		cursor := newChatStreamCursor(offset)
		require.Equal(t, want, cursor.suffix("你🌟好"))
		require.Equal(t, "", cursor.suffix("你🌟好"))
		require.Equal(t, "，世界🚀", cursor.suffix("你🌟好，世界🚀"))
		require.Equal(t, 7, cursor.runes)
	}
	cursor := newChatStreamCursor(4)
	require.Empty(t, cursor.suffix("你🌟好"))
	require.Equal(t, "新", cursor.suffix("你🌟好！新"))
}

func TestChatLiveSnapshotDoesNotShareMutableTools(t *testing.T) {
	live := newChatToolLive()
	_, changed := live.snapshot()
	live.publish("你好", "思考🌟", nil)
	select {
	case <-changed:
	default:
		t.Fatal("publishing did not wake the reader")
	}
	before, _ := live.snapshot()
	tools := []service.InternalToolEvent{{ID: "image", Output: "原始工具结果"}}
	live.publish("你好世界", "思考🌟继续", tools)
	tools[0].Output = "外部修改"
	copy, _ := live.snapshot()
	require.Equal(t, "原始工具结果", copy.tools[0].Output)
	copy.tools[0].Output = "读取者修改"
	again, _ := live.snapshot()
	require.Equal(t, "原始工具结果", again.tools[0].Output)
	require.Equal(t, "你好", before.content)
	require.Equal(t, uint64(1), before.version)
	live.finish()
	final, finished := live.snapshot()
	require.True(t, final.finished)
	select {
	case <-finished:
	default:
		t.Fatal("late subscribers must see completion without another polling delay")
	}
}

func TestChatLiveDoesNotExposeAnotherAccountSnapshot(t *testing.T) {
	db, mock, err := sqlmock.New()
	require.NoError(t, err)
	defer func() { _ = db.Close() }()
	job := uuid.NewString()
	mock.ExpectExec("UPDATE chat_generations SET status='failed'").WithArgs(int64(42), job).WillReturnResult(sqlmock.NewResult(0, 0))
	mock.ExpectQuery("SELECT g.id,g.status").WithArgs(int64(42), job, 1, 1).WillReturnError(sql.ErrNoRows)
	h := &ChatHistoryHandler{db: db}
	live := newChatToolLive()
	live.publish("另一账号的回复", "另一账号的思考", nil)
	h.liveTools.Store("43:"+job, live)
	c, w := historyTestContext("GET", "/generations/"+job+"/events", "")
	c.Params = gin.Params{{Key: "id", Value: job}}
	h.GenerationEvents(c)
	require.NotContains(t, w.Body.String(), "另一账号")
	require.Contains(t, w.Body.String(), "回复连接暂时中断")
	require.NoError(t, mock.ExpectationsWereMet())
}

type liveTestFrame struct {
	Generation chatGeneration `json:"generation"`
	Content    string         `json:"content"`
	Reasoning  string         `json:"reasoning"`
}

func nextLiveFrame(t *testing.T, frames <-chan liveTestFrame) liveTestFrame {
	t.Helper()
	select {
	case frame := <-frames:
		return frame
	case <-time.After(250 * time.Millisecond):
		t.Fatal("live output waited for a durable checkpoint or polling interval")
		return liveTestFrame{}
	}
}

func TestChatLiveSSEStreamsBeforeCheckpointAndFinishesImmediately(t *testing.T) {
	gin.SetMode(gin.TestMode)
	db, mock, err := sqlmock.New()
	require.NoError(t, err)
	defer func() { _ = db.Close() }()
	job := uuid.NewString()
	mock.ExpectExec("UPDATE chat_generations SET status='failed'").WithArgs(int64(42), job).WillReturnResult(sqlmock.NewResult(0, 0))
	columns := []string{"id", "status", "sequence", "error", "content", "reasoning", "tools"}
	mock.ExpectQuery("SELECT g.id,g.status").WithArgs(int64(42), job, 1, 1).WillReturnRows(sqlmock.NewRows(columns).AddRow(job, "running", 0, "", "", "", "[]"))
	mock.ExpectQuery("SELECT g.id,g.status").WithArgs(int64(42), job, 8, 7).WillReturnRows(sqlmock.NewRows(columns).AddRow(job, "completed", 1, "", "", "", "[]"))
	h := &ChatHistoryHandler{db: db}
	live := newChatToolLive()
	h.liveTools.Store("42:"+job, live)
	router := gin.New()
	router.GET("/generations/:id/events", func(c *gin.Context) {
		c.Set(string(middleware.ContextKeyUser), middleware.AuthSubject{UserID: 42})
		h.GenerationEvents(c)
	})
	server := httptest.NewServer(router)
	defer server.Close()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	request, err := http.NewRequestWithContext(ctx, "GET", server.URL+"/generations/"+job+"/events", nil)
	require.NoError(t, err)
	response, err := server.Client().Do(request)
	require.NoError(t, err)
	defer func() { _ = response.Body.Close() }()
	require.Equal(t, "no", response.Header.Get("X-Accel-Buffering"))
	frames := make(chan liveTestFrame, 16)
	go func() {
		scanner := bufio.NewScanner(response.Body)
		for scanner.Scan() {
			if !strings.HasPrefix(scanner.Text(), "data: ") {
				continue
			}
			var frame liveTestFrame
			if json.Unmarshal([]byte(strings.TrimPrefix(scanner.Text(), "data: ")), &frame) == nil {
				frames <- frame
			}
		}
	}()
	require.Equal(t, "running", nextLiveFrame(t, frames).Generation.Status)
	started := time.Now()
	live.publish("你好🌟", "先思考", nil)
	first := nextLiveFrame(t, frames)
	firstDelay := time.Since(started)
	require.Equal(t, "你好🌟", first.Content)
	require.Equal(t, "先思考", first.Reasoning)
	require.Less(t, firstDelay, 100*time.Millisecond)
	live.publish("你好🌟，世界🚀", "先思考再回答", nil)
	next := nextLiveFrame(t, frames)
	require.Equal(t, "，世界🚀", next.Content)
	require.Equal(t, "再回答", next.Reasoning)
	finished := time.Now()
	h.liveTools.Delete("42:" + job)
	live.finish()
	require.Equal(t, "completed", nextLiveFrame(t, frames).Generation.Status)
	require.Less(t, time.Since(finished), 100*time.Millisecond)
	require.NoError(t, mock.ExpectationsWereMet())
	t.Logf("live publish-to-SSE first-output latency: %.2f ms", float64(firstDelay.Microseconds())/1000)
}

func TestChatLiveSSEReconnectsAtUnicodeOffsets(t *testing.T) {
	db, mock, err := sqlmock.New()
	require.NoError(t, err)
	defer func() { _ = db.Close() }()
	job := uuid.NewString()
	mock.ExpectExec("UPDATE chat_generations SET status='failed'").WithArgs(int64(42), job).WillReturnResult(sqlmock.NewResult(0, 0))
	mock.ExpectQuery("SELECT g.id,g.status").WithArgs(int64(42), job, 3, 2).WillReturnRows(sqlmock.NewRows([]string{"id", "status", "sequence", "error", "content", "reasoning", "tools"}).AddRow(job, "running", 0, "", "", "", "[]"))
	h := &ChatHistoryHandler{db: db}
	live := newChatToolLive()
	live.publish("你🌟好！", "想清楚", nil)
	h.liveTools.Store("42:"+job, live)
	c, w := historyTestContext("GET", fmt.Sprintf("/generations/%s/events?content_offset=2&reasoning_offset=1", job), "")
	c.Params = gin.Params{{Key: "id", Value: job}}
	ctx, cancel := context.WithCancel(c.Request.Context())
	c.Request = c.Request.WithContext(ctx)
	time.AfterFunc(30*time.Millisecond, cancel)
	h.GenerationEvents(c)
	require.Contains(t, w.Body.String(), `"content":"好！"`)
	require.Contains(t, w.Body.String(), `"reasoning":"清楚"`)
	require.NotContains(t, w.Body.String(), "你🌟")
	require.NoError(t, mock.ExpectationsWereMet())
}

func TestChatLiveStopPersistsTokensNewerThanCheckpoint(t *testing.T) {
	db, mock, err := sqlmock.New()
	require.NoError(t, err)
	defer func() { _ = db.Close() }()
	job, chat := uuid.NewString(), uuid.NewString()
	h := &ChatHistoryHandler{db: db, tasks: make(map[string]context.CancelFunc)}
	live := newChatToolLive()
	live.publish("完整可见回答🌟", "完整思考", nil)
	h.liveTools.Store("42:"+job, live)
	mock.ExpectBegin()
	mock.ExpectExec("SELECT pg_advisory_xact_lock").WithArgs(int64(42)).WillReturnResult(sqlmock.NewResult(0, 1))
	mock.ExpectQuery("SELECT conversation_id::text,content,reasoning,tools,status").WithArgs(int64(42), job).WillReturnRows(sqlmock.NewRows([]string{"conversation_id", "content", "reasoning", "tools", "status"}).AddRow(chat, "旧检查点", "旧思考", "[]", "running"))
	mock.ExpectExec("UPDATE chat_conversations SET messages=").WithArgs(int64(42), chat, job, "完整可见回答🌟", "完整思考", "[]").WillReturnResult(sqlmock.NewResult(0, 1))
	mock.ExpectExec("UPDATE chat_generations SET status='stopped'").WithArgs(int64(42), job).WillReturnResult(sqlmock.NewResult(0, 1))
	mock.ExpectCommit()
	c, w := historyTestContext("POST", "/generations/"+job+"/stop", "")
	c.Params = gin.Params{{Key: "id", Value: job}}
	h.StopGeneration(c)
	require.Equal(t, http.StatusOK, w.Code)
	require.NoError(t, mock.ExpectationsWereMet())
}
