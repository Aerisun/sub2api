package handler

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/DATA-DOG/go-sqlmock"
	"github.com/Wei-Shaw/sub2api/internal/service"
	"github.com/gin-gonic/gin"
	"github.com/google/uuid"
	"github.com/stretchr/testify/require"
)

func historyAgentFixture() (chatRecord, []chatImage) {
	images := []chatImage{{ClientID: uuid.NewString(), Name: "first.png", MediaType: "image/png"}, {ClientID: uuid.NewString(), Name: "second.png", MediaType: "image/png"}}
	item := chatRecord{ID: uuid.NewString(), Model: "Nemotron-3.5", Messages: []chatMessage{
		{Role: "user", Content: "介绍两张图", Images: images},
		{Role: "assistant", Content: "已介绍", Tools: []service.InternalToolEvent{{Type: "tool_call", ID: "old", Name: "image_recognition", Status: "completed", Output: "图片1为红色三角形，图片2为蓝色圆形", Input: mustChatJSON(chatImageToolInput{Images: images})}}},
		{Role: "user", Content: "看看第二张图里更小的文字"}, {ID: uuid.NewString(), Role: "assistant"}}}
	return item, images
}
func emitFixtureCall(w http.ResponseWriter, calls []chatModelCall) {
	w.Header().Set("Content-Type", "text/event-stream")
	for index, call := range calls {
		// Names and arguments may arrive in separate chunks, through the agent.
		a := map[string]any{"index": index, "id": call.ID, "type": "function", "function": map[string]string{"name": "aerisun_read_", "arguments": ""}}
		b := map[string]any{"index": index, "function": map[string]string{"name": "chat_images", "arguments": call.Function.Arguments}}
		for _, fragment := range []any{a, b} {
			fmt.Fprintf(w, "data: %s\n\n", mustChatJSON(map[string]any{"choices": []any{map[string]any{"index": 0, "delta": map[string]any{"tool_calls": []any{fragment}}}}}))
		}
	}
	_, _ = io.WriteString(w, "data: [DONE]\n\n")
}
func emitFixtureAnswer(w http.ResponseWriter, text string) {
	w.Header().Set("Content-Type", "text/event-stream")
	fmt.Fprintf(w, "data: %s\n\ndata: [DONE]\n\n", mustChatJSON(map[string]any{"choices": []any{map[string]any{"delta": map[string]string{"content": text}}}}))
}

func TestChatHistoryAgentReusesResultsInOneModelRequestWithoutReadingAnyOriginal(t *testing.T) {
	db, mock, err := sqlmock.New()
	require.NoError(t, err)
	defer func() { _ = db.Close() }()
	item, _ := historyAgentFixture()
	item.Messages[2].Content = "沿用已有结果，告诉我第二张图的形状"
	var calls atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		require.Equal(t, "/api/v1/chat/completions", r.URL.Path)
		raw, _ := io.ReadAll(r.Body)
		require.Contains(t, string(raw), chatHistoryVisionFunction)
		require.Contains(t, string(raw), "image_2")
		require.Contains(t, string(raw), "蓝色圆形")
		require.NotContains(t, string(raw), "data:image")
		emitFixtureAnswer(w, "第二张是蓝色圆形")
	}))
	defer server.Close()
	h := &ChatHistoryHandler{db: db, baseURL: server.URL, toolURL: server.URL, client: server.Client()}
	out := make(chan chatDelta, 16)
	h.modelStream(context.Background(), 42, item, "Bearer fixture", nil, out)
	require.Equal(t, "第二张是蓝色圆形", (<-out).Content)
	require.True(t, (<-out).Done)
	require.EqualValues(t, 1, calls.Load())
	require.NoError(t, mock.ExpectationsWereMet())
}

func TestChatHistoryAgentModelChoosesAndBrowserSuppliesOnlyTheRequestedImage(t *testing.T) {
	db, mock, err := sqlmock.New()
	require.NoError(t, err)
	defer func() { _ = db.Close() }()
	item, images := historyAgentFixture()
	job := item.Messages[3].ID
	data := "data:image/png;base64," + base64.StdEncoding.EncodeToString([]byte("\x89PNG\r\n\x1a\nsecond-only"))
	var rounds, recognitions, callbacks atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/v1/tools/image_recognition" {
			recognitions.Add(1)
			var body struct {
				Image string `json:"image"`
			}
			require.NoError(t, json.NewDecoder(r.Body).Decode(&body))
			require.Equal(t, data, body.Image)
			_, _ = io.WriteString(w, `{"name":"image_recognition","status":"completed","output":{"text":"小字为 code73","image_count":1}}`)
			return
		}
		var payload map[string]any
		require.NoError(t, json.NewDecoder(r.Body).Decode(&payload))
		require.Equal(t, "chat_"+item.ID, r.Header.Get("X-Session-Id"))
		if rounds.Add(1) == 1 {
			require.NotNil(t, payload["tools"])
			call := chatModelCall{ID: "read-one", Type: "function", Function: chatModelFunction{Name: chatHistoryVisionFunction, Arguments: `{"image_ids":["image_2","image_2"],"prompt":"读出小字"}`}}
			emitFixtureCall(w, []chatModelCall{call, {ID: "read-two", Type: "function", Function: call.Function}})
			return
		}
		require.Nil(t, payload["tools"])
		raw := string(mustChatJSON(payload))
		require.NotContains(t, raw, "data:image")
		require.Contains(t, raw, "code73")
		require.Contains(t, raw, "read-two")
		require.Contains(t, raw, "tool_call_id")
		require.Contains(t, r.Header.Get("Idempotency-Key"), "-vision-1")
		emitFixtureAnswer(w, "小字是 code73")
	}))
	defer server.Close()
	h := &ChatHistoryHandler{db: db, baseURL: server.URL, toolURL: server.URL, client: server.Client()}
	emit := func(delta chatDelta) bool {
		if delta.ImageRequest != nil && delta.ImageRequest.ID != "" {
			callbacks.Add(1)
			require.Equal(t, []string{images[1].ClientID}, delta.ImageRequest.ImageIDs)
			mock.ExpectQuery("SELECT EXISTS").WithArgs(int64(42), job).WillReturnRows(sqlmock.NewRows([]string{"exists"}).AddRow(true))
			body := string(mustChatJSON(map[string]any{"requestID": delta.ImageRequest.ID, "images": map[string]string{images[1].ClientID: data}}))
			c, w := historyTestContext("POST", "/generations/"+job+"/image-inputs", body)
			c.Params = gin.Params{{Key: "id", Value: job}}
			h.ImageInputs(c)
			require.Equal(t, 200, w.Code)
		}
		if delta.Tool != nil {
			require.NotContains(t, string(delta.Tool.Input), "data:image")
			require.NotContains(t, string(delta.Tool.Input), images[0].ClientID)
		}
		return true
	}
	messages := []map[string]any{{"role": "user", "content": "读小字"}}
	err = h.runChatImageAgent(context.Background(), 42, item, map[string]any{"model": item.Model, "messages": messages, "stream": true}, chatHistoricalImageReferences(item.Messages), 12, "Bearer fixture", emit)
	require.NoError(t, err)
	require.EqualValues(t, 2, rounds.Load())
	require.EqualValues(t, 1, recognitions.Load())
	require.EqualValues(t, 1, callbacks.Load())
	require.NoError(t, mock.ExpectationsWereMet())
	_, exists := h.imageRequests.Load("42:" + job)
	require.False(t, exists)
}

func TestChatHistoryAgentUnavailableOrForeignImagesDoNotReachPaidRecognition(t *testing.T) {
	for _, foreign := range []bool{false, true} {
		t.Run(fmt.Sprint(foreign), func(t *testing.T) {
			item, images := historyAgentFixture()
			var modelCalls atomic.Int32
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				require.Equal(t, "/api/v1/chat/completions", r.URL.Path, "a paid recognition must not start")
				if modelCalls.Add(1) == 1 {
					id := "image_1"
					if foreign {
						id = "image_999"
					}
					emitFixtureCall(w, []chatModelCall{{ID: "read", Type: "function", Function: chatModelFunction{Arguments: fmt.Sprintf(`{"image_ids":["%s"],"prompt":"看原图"}`, id)}}})
					return
				}
				raw, _ := io.ReadAll(r.Body)
				require.NotContains(t, string(raw), "data:image")
				emitFixtureAnswer(w, "已有描述中第一张是红色三角形")
			}))
			defer server.Close()
			h := &ChatHistoryHandler{baseURL: server.URL, toolURL: server.URL, client: server.Client()}
			failed := 0
			emit := func(delta chatDelta) bool {
				if delta.ImageRequest != nil && delta.ImageRequest.ID != "" {
					require.False(t, foreign)
					value, ok := h.imageRequests.Load("42:" + item.Messages[3].ID)
					require.True(t, ok)
					mailbox, mailboxOK := value.(*chatImageMailbox)
					require.True(t, mailboxOK)
					mailbox.delivery <- chatImageDelivery{Unavailable: []string{images[0].ClientID}}
				}
				if delta.Tool != nil {
					require.Equal(t, "failed", delta.Tool.Status)
					failed++
				}
				return true
			}
			err := h.runChatImageAgent(context.Background(), 42, item, map[string]any{"messages": []map[string]any{{"role": "user", "content": "看图"}}}, chatHistoricalImageReferences(item.Messages), 12, "Bearer fixture", emit)
			require.NoError(t, err)
			require.EqualValues(t, 2, modelCalls.Load())
			require.Equal(t, 1, failed)
		})
	}
}

func TestChatImageMailboxIsOwnedValidatedAndIdempotent(t *testing.T) {
	db, mock, err := sqlmock.New()
	require.NoError(t, err)
	defer func() { _ = db.Close() }()
	job := uuid.NewString()
	image := chatImage{ClientID: uuid.NewString(), Name: "only.png", MediaType: "image/png"}
	box := &chatImageMailbox{id: uuid.NewString(), images: []chatImage{image}, delivery: make(chan chatImageDelivery, 1)}
	h := &ChatHistoryHandler{db: db}
	h.imageRequests.Store("42:"+job, box)
	post := func(requestID string, images map[string]string, owned bool) int {
		mock.ExpectQuery("SELECT EXISTS").WithArgs(int64(42), job).WillReturnRows(sqlmock.NewRows([]string{"exists"}).AddRow(owned))
		c, w := historyTestContext("POST", "/generations/"+job+"/image-inputs", string(mustChatJSON(map[string]any{"requestID": requestID, "images": images})))
		c.Params = gin.Params{{Key: "id", Value: job}}
		h.ImageInputs(c)
		return w.Code
	}
	require.Equal(t, 404, post(box.id, nil, false))
	require.Equal(t, 400, post(uuid.NewString(), nil, true))
	require.Equal(t, 400, post(box.id, map[string]string{uuid.NewString(): "data:image/png;base64,AAAA"}, true))
	data := "data:image/png;base64," + base64.StdEncoding.EncodeToString([]byte("\x89PNG\r\n\x1a\nfixture"))
	for i := 0; i < 2; i++ {
		require.Equal(t, 200, post(box.id, map[string]string{image.ClientID: data}, true))
	}
	require.Len(t, box.delivery, 1, "reconnecting readers cannot submit a second recognition")
	require.NoError(t, mock.ExpectationsWereMet())
}

func TestChatBrowserImageRequestCancellationCleansTransientState(t *testing.T) {
	h := &ChatHistoryHandler{}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	job := uuid.NewString()
	ready := make(chan struct{})
	done := make(chan error, 1)
	go func() {
		_, _, err := h.requestChatBrowserImages(ctx, 42, job, []chatImage{{ClientID: uuid.NewString()}}, func(delta chatDelta) bool {
			if delta.ImageRequest.ID != "" {
				close(ready)
			}
			return true
		})
		done <- err
	}()
	<-ready
	cancel()
	select {
	case err := <-done:
		require.ErrorIs(t, err, context.Canceled)
	case <-time.After(time.Second):
		t.Fatal("browser wait did not cancel")
	}
	_, exists := h.imageRequests.Load("42:" + job)
	require.False(t, exists)
	live := newChatToolLive()
	request := &chatImageRequest{ID: "request", ImageIDs: []string{"one"}}
	live.publish("", "", nil, request)
	snapshot, _ := live.snapshot()
	require.Equal(t, request, snapshot.imageRequest)
	live.publish("", "", nil, (*chatImageRequest)(nil))
	snapshot, _ = live.snapshot()
	require.Nil(t, snapshot.imageRequest)
}

func TestChatHistoryToolArgumentsStayBounded(t *testing.T) {
	item, _ := historyAgentFixture()
	h := &ChatHistoryHandler{}
	for _, arguments := range []string{`{broken`, `{"image_ids":[],"prompt":"x"}`, `{"image_ids":["image_1","image_2"],"prompt":"x"}`} {
		result := h.executeChatHistoryVision(context.Background(), 42, item, chatHistoricalImageReferences(item.Messages), []chatModelCall{{Function: chatModelFunction{Arguments: arguments}}}, 1, "", func(delta chatDelta) bool { require.Nil(t, delta.ImageRequest); return true })
		require.Equal(t, "failed", result["status"])
		require.NotContains(t, strings.TrimSpace(string(mustChatJSON(result))), "data:image")
	}
}
