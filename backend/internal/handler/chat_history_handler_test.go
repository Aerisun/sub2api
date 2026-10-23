package handler

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"regexp"
	"strings"
	"testing"

	"github.com/DATA-DOG/go-sqlmock"
	"github.com/Wei-Shaw/sub2api/internal/server/middleware"
	"github.com/Wei-Shaw/sub2api/internal/service"
	"github.com/gin-gonic/gin"
	"github.com/google/uuid"
	"github.com/stretchr/testify/require"
)

func historyTestContext(method, path, body string) (*gin.Context, *httptest.ResponseRecorder) {
	gin.SetMode(gin.TestMode)
	w := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(w)
	c.Request = httptest.NewRequest(method, path, strings.NewReader(body))
	c.Request.Header.Set("Content-Type", "application/json")
	c.Set(string(middleware.ContextKeyUser), middleware.AuthSubject{UserID: 42})
	return c, w
}
func TestChatHistoryImageIsAccountScoped(t *testing.T) {
	db, mock, err := sqlmock.New()
	require.NoError(t, err)
	defer db.Close()
	asset := uuid.NewString()
	c, w := historyTestContext("GET", "/images/"+asset, "")
	c.Params = gin.Params{{Key: "id", Value: asset}}
	mock.ExpectQuery("SELECT media_type,content FROM chat_images WHERE user_id=\\$1 AND id=\\$2").WithArgs(int64(42), asset).WillReturnRows(sqlmock.NewRows([]string{"media_type", "content"}))
	(&ChatHistoryHandler{db: db}).Image(c)
	require.Equal(t, 404, w.Code)
	require.NoError(t, mock.ExpectationsWereMet())
}
func TestChatHistoryRejectsRawImageAndOversizedReferences(t *testing.T) {
	item := chatRecord{Title: "图像", Model: "test", Messages: []chatMessage{{Role: "user", Images: []chatImage{{AssetID: uuid.NewString(), MediaType: "image/png", URL: "data:image/png;base64,AAAA"}}}}}
	require.False(t, validChatRecord(&item))
	item.Messages[0].Images[0].URL = ""
	require.True(t, validChatRecord(&item))
	item.Messages[0].Images[0].AssetID = "not-a-uuid"
	require.False(t, validChatRecord(&item))
}
func TestChatHistoryRetriesDoNotStartAnotherGeneration(t *testing.T) {
	db, mock, err := sqlmock.New()
	require.NoError(t, err)
	defer db.Close()
	chatID, jobID := uuid.NewString(), uuid.NewString()
	item := chatRecord{Title: "验证", Model: "qwen", Messages: []chatMessage{{Role: "user", Content: "hi"}, {ID: jobID, Role: "assistant"}}}
	body, _ := json.Marshal(map[string]any{"chat": item, "requestID": jobID})
	c, w := historyTestContext("POST", "/conversations/"+chatID+"/generations", string(body))
	c.Params = gin.Params{{Key: "id", Value: chatID}}
	mock.ExpectQuery("SELECT EXISTS").WithArgs(int64(42), jobID, chatID).WillReturnRows(sqlmock.NewRows([]string{"exists"}).AddRow(true))
	mock.ExpectExec("UPDATE chat_generations SET status='failed'").WithArgs(int64(42), chatID).WillReturnResult(sqlmock.NewResult(0, 0))
	mock.ExpectQuery("SELECT c.id,c.title,c.model,c.version").WithArgs(int64(42), chatID).WillReturnRows(sqlmock.NewRows([]string{"id", "title", "model", "version", "updated", "messages", "job_id", "status", "sequence", "error", "content", "reasoning", "tools"}).AddRow(chatID, "验证", "qwen", 1, 123, mustChatJSON(item.Messages), jobID, "running", 2, "", "已有内容", "思考中", "[]"))
	(&ChatHistoryHandler{db: db}).Generate(c)
	require.Equal(t, 200, w.Code)
	require.Contains(t, w.Body.String(), "已有内容")
	require.NoError(t, mock.ExpectationsWereMet())
}
func TestChatHistoryModelStreamUsesOwnedAssetsAndRegularBillingRoute(t *testing.T) {
	db, mock, err := sqlmock.New()
	require.NoError(t, err)
	defer db.Close()
	asset := uuid.NewString()
	chatID := uuid.NewString()
	png := []byte("image bytes")
	mock.ExpectQuery("SELECT media_type,content").WithArgs(int64(42), asset).WillReturnRows(sqlmock.NewRows([]string{"media_type", "content"}).AddRow("image/png", png))
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		require.Equal(t, "/api/v1/chat/completions", r.URL.Path)
		require.Equal(t, "Bearer user-jwt", r.Header.Get("Authorization"))
		require.Equal(t, "chat_"+chatID, r.Header.Get("X-Session-Id"))
		body, _ := io.ReadAll(r.Body)
		require.Contains(t, string(body), "image_url")
		require.NotContains(t, string(body), "evil")
		require.Contains(t, string(body), `"service_tier":"fast"`)
		require.Contains(t, string(body), `"reasoning_effort":"max"`)
		w.Header().Set("Content-Type", "text/event-stream")
		_, _ = io.Copy(w, bytes.NewBufferString("data: {\"choices\":[{\"delta\":{\"content\":\"红色圆形\"}}]}\n\ndata: [DONE]\n\n"))
	}))
	defer upstream.Close()
	h := &ChatHistoryHandler{db: db, baseURL: upstream.URL, client: upstream.Client()}
	out := make(chan chatDelta, 4)
	h.modelStream(context.Background(), 42, chatRecord{ID: chatID, Model: "gpt-6.1-sol", Messages: []chatMessage{{Role: "user", Images: []chatImage{{AssetID: asset}}}, {Role: "assistant"}}}, "Bearer user-jwt", map[string]json.RawMessage{"evil": json.RawMessage(`true`), "service_tier": json.RawMessage(`"fast"`), "reasoning_effort": json.RawMessage(`"max"`)}, out)
	require.Equal(t, "红色圆形", (<-out).Content)
	require.True(t, (<-out).Done)
	require.NoError(t, mock.ExpectationsWereMet())
}

func TestChatHistoryGenerationOffsetsAreExplicitIntegerPositions(t *testing.T) {
	db, mock, err := sqlmock.New()
	require.NoError(t, err)
	defer db.Close()
	jobID := uuid.NewString()
	c, w := historyTestContext("GET", "/generations/"+jobID+"?content_offset=2&reasoning_offset=1", "")
	c.Params = gin.Params{{Key: "id", Value: jobID}}
	mock.ExpectQuery(regexp.QuoteMeta(`substring(g.content from $3::int),substring(g.reasoning from $4::int)`)).WithArgs(int64(42), jobID, 3, 2).WillReturnRows(sqlmock.NewRows([]string{"id", "status", "sequence", "error", "content", "reasoning", "tools"}).AddRow(jobID, "running", 4, "", "新增", "思考", "[]"))
	(&ChatHistoryHandler{db: db}).Generation(c)
	require.Equal(t, 200, w.Code)
	require.Contains(t, w.Body.String(), "新增")
	require.NoError(t, mock.ExpectationsWereMet())
}

func TestChatHistoryDeleteChecksRunningJobInsideTheGenerationLock(t *testing.T) {
	db, mock, err := sqlmock.New()
	require.NoError(t, err)
	defer db.Close()
	chatID := uuid.NewString()
	c, w := historyTestContext("DELETE", "/conversations/"+chatID, "")
	c.Params = gin.Params{{Key: "id", Value: chatID}}
	mock.ExpectBegin()
	mock.ExpectExec(regexp.QuoteMeta(`SELECT pg_advisory_xact_lock($1)`)).WithArgs(int64(42)).WillReturnResult(sqlmock.NewResult(0, 1))
	mock.ExpectQuery("SELECT EXISTS").WithArgs(int64(42), chatID).WillReturnRows(sqlmock.NewRows([]string{"active"}).AddRow(true))
	mock.ExpectRollback()
	(&ChatHistoryHandler{db: db}).Delete(c)
	require.Equal(t, 409, w.Code)
	require.NoError(t, mock.ExpectationsWereMet())
}

func TestChatHistoryFollowupNeverLoadsOldImagesAndReusesToolDescription(t *testing.T) {
	db, mock, err := sqlmock.New()
	require.NoError(t, err)
	defer db.Close()
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		raw, _ := io.ReadAll(r.Body)
		require.NotContains(t, string(raw), "image_url")
		require.Contains(t, string(raw), "红色三角形")
		require.NotContains(t, string(raw), "本轮不重新识别")
		require.NotContains(t, string(raw), "此前图片识别结果")
		w.Header().Set("Content-Type", "text/event-stream")
		_, _ = io.WriteString(w, "data: [DONE]\n\n")
	}))
	defer upstream.Close()
	item := chatRecord{Model: "Nemotron-3.5", Messages: []chatMessage{
		{Role: "user", Images: []chatImage{{AssetID: uuid.NewString()}}},
		{Role: "assistant", Content: "已经识别", Tools: []service.InternalToolEvent{{Type: "tool_call", ID: "v1", Name: "image_recognition", Status: "completed", Output: "红色三角形"}}},
		{Role: "user", Content: "给我一段代码"}, {Role: "assistant"},
	}}
	h := &ChatHistoryHandler{db: db, baseURL: upstream.URL, client: upstream.Client()}
	out := make(chan chatDelta, 2)
	h.modelStream(context.Background(), 42, item, "Bearer user-jwt", nil, out)
	require.True(t, (<-out).Done)
	require.NoError(t, mock.ExpectationsWereMet())
}

func TestChatHistoryNativeVisionKeepsImagePrefixAcrossFollowups(t *testing.T) {
	db, mock, err := sqlmock.New()
	require.NoError(t, err)
	defer db.Close()
	asset := uuid.NewString()
	var requests []struct {
		Messages []json.RawMessage `json:"messages"`
	}
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var payload struct {
			Messages []json.RawMessage `json:"messages"`
		}
		require.NoError(t, json.NewDecoder(r.Body).Decode(&payload))
		requests = append(requests, payload)
		w.Header().Set("Content-Type", "text/event-stream")
		_, _ = io.WriteString(w, "data: [DONE]\n\n")
	}))
	defer upstream.Close()
	h := &ChatHistoryHandler{db: db, baseURL: upstream.URL, client: upstream.Client()}
	item := chatRecord{ID: uuid.NewString(), Model: "gpt-6.1-sol", Messages: []chatMessage{
		{Role: "user", Content: "Describe this image", Images: []chatImage{{AssetID: asset}}},
		{Role: "assistant"},
	}}
	for turn := 0; turn < 3; turn++ {
		mock.ExpectQuery("SELECT media_type,content").WithArgs(int64(42), asset).
			WillReturnRows(sqlmock.NewRows([]string{"media_type", "content"}).AddRow("image/png", []byte("immutable image")))
		out := make(chan chatDelta, 2)
		h.modelStream(context.Background(), 42, item, "Bearer user-jwt", nil, out)
		require.True(t, (<-out).Done)
		item.Messages[len(item.Messages)-1].Content = "Blue circle"
		item.Messages = append(item.Messages, chatMessage{Role: "user", Content: "What shape?"}, chatMessage{Role: "assistant"})
	}
	require.Len(t, requests, 3)
	require.Contains(t, string(requests[0].Messages[0]), "data:image/png;base64,")
	for _, request := range requests[1:] {
		require.JSONEq(t, string(requests[0].Messages[0]), string(request.Messages[0]))
	}
	require.NoError(t, mock.ExpectationsWereMet())
}
