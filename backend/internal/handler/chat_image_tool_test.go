package handler

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/DATA-DOG/go-sqlmock"
	"github.com/Wei-Shaw/sub2api/internal/service"
	"github.com/google/uuid"
	"github.com/stretchr/testify/require"
)

func TestImageRecognitionRejectsMultipleImagesBeforeForwarding(t *testing.T) {
	for _, count := range []int{0, 2, 4} {
		parts := make([]any, count)
		for i := range parts {
			parts[i] = map[string]any{"type": "image_url", "image_url": map[string]any{"url": "data:image/png;base64,fixture"}}
		}
		body := mustChatJSON(map[string]any{"model": service.ToolVisionModel, "messages": []any{map[string]any{"role": "user", "content": parts}}})
		c, response := historyTestContext("POST", "/api/v1/chat/images/recognitions", string(body))
		h := &OpenAIGatewayHandler{}
		h.ImageRecognition(c)
		require.Equal(t, 400, response.Code)
		require.Contains(t, response.Body.String(), "exactly one image")
	}
}

func TestChatRecognitionTransientImagesNeverNeedImageStorage(t *testing.T) {
	db, mock, err := sqlmock.New()
	require.NoError(t, err)
	defer db.Close()
	image := chatImage{ClientID: uuid.NewString(), Name: "local.png", MediaType: "image/png"}
	url := "data:image/png;base64," + base64.StdEncoding.EncodeToString([]byte("\x89PNG\r\n\x1a\nimage"))
	require.NoError(t, validateChatImageInputs([]chatImage{image}, map[string]string{image.ClientID: url}))
	for _, inputs := range []map[string]string{nil, {image.ClientID: "https://example.com/image.png"}, {uuid.NewString(): url}, {image.ClientID: "data:image/png;base64,ZmFrZQ=="}} {
		require.Error(t, validateChatImageInputs([]chatImage{image}, inputs))
	}
	tool := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		require.Equal(t, "/v1/tools/image_recognition", r.URL.Path)
		var body struct {
			Image string `json:"image"`
		}
		require.NoError(t, json.NewDecoder(r.Body).Decode(&body))
		require.Equal(t, url, body.Image)
		_, _ = io.WriteString(w, `{"name":"image_recognition","status":"completed","output":{"text":"红色三角形","image_count":1}}`)
	}))
	defer tool.Close()
	model := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		require.NotContains(t, string(body), "base64")
		require.Contains(t, string(body), "红色三角形")
		w.Header().Set("Content-Type", "text/event-stream")
		_, _ = io.WriteString(w, "data: [DONE]\n\n")
	}))
	defer model.Close()
	h := &ChatHistoryHandler{db: db, baseURL: model.URL, toolURL: tool.URL, client: model.Client()}
	ctx := context.WithValue(context.Background(), chatImageInputsKey{}, map[string]string{image.ClientID: url})
	out := make(chan chatDelta, 4)
	item := chatRecord{Title: "本地图片", Model: "Nemotron-3.5", Messages: []chatMessage{{Role: "user", Images: []chatImage{image}}, {Role: "assistant"}}}
	require.True(t, validChatRecord(&item))
	h.modelStream(ctx, 42, item, "Bearer user-jwt", nil, out)
	require.Equal(t, "running", (<-out).Tool.Status)
	completed := (<-out).Tool
	require.Equal(t, "completed", completed.Status)
	require.Contains(t, string(completed.Input), image.ClientID)
	require.NotContains(t, string(completed.Input), "base64")
	require.NotContains(t, string(mustChatJSON(item)), "base64")
	require.True(t, (<-out).Done)
	require.NoError(t, mock.ExpectationsWereMet(), "transient recognition must not query chat_images")
}

func TestChatBrowserOnlyHistoryDoesNotRequireOriginalImageOnAnotherDevice(t *testing.T) {
	db, mock, err := sqlmock.New()
	require.NoError(t, err)
	defer db.Close()
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		require.Equal(t, "/api/v1/chat/completions", r.URL.Path)
		body, _ := io.ReadAll(r.Body)
		require.NotContains(t, string(body), "image_url")
		require.Contains(t, string(body), "红色三角形")
		w.Header().Set("Content-Type", "text/event-stream")
		_, _ = io.WriteString(w, "data: [DONE]\n\n")
	}))
	defer server.Close()
	image := chatImage{ClientID: uuid.NewString(), Name: "local.png", MediaType: "image/png"}
	h := &ChatHistoryHandler{db: db, baseURL: server.URL, toolURL: server.URL, client: server.Client()}
	item := chatRecord{Model: "Nemotron-3.5", Messages: []chatMessage{
		{Role: "user", Images: []chatImage{image}},
		{Role: "assistant", Tools: []service.InternalToolEvent{{Type: "tool_call", ID: "local", Name: "image_recognition", Status: "completed", Output: "红色三角形", Input: mustChatJSON(chatImageToolInput{Images: []chatImage{image}})}}},
		{Role: "user", Content: "继续"}, {Role: "assistant"},
	}}
	out := make(chan chatDelta, 2)
	h.modelStream(context.Background(), 42, item, "Bearer user-jwt", nil, out)
	require.True(t, (<-out).Done)
	require.NoError(t, mock.ExpectationsWereMet())
}

func TestChatNativeVisionCatalogueIncludesOnlyVerifiedNativeModels(t *testing.T) {
	data, err := os.ReadFile("../../../frontend/src/config/aerisunModels.json")
	require.NoError(t, err)
	var catalogue map[string]json.RawMessage
	require.NoError(t, json.Unmarshal(data, &catalogue))
	helperModels := map[string]bool{"glm-5.2": true, "glm-5.3": true, "Nemotron-3.5": true}
	for model := range catalogue {
		require.Equal(t, !helperModels[model], chatModelHasNativeVision(model), model)
		require.Equal(t, !helperModels[model], chatModelHasNativeVision("provider/"+strings.ToUpper(model)), model)
	}
	for _, model := range []string{"unknown-model", "nemotron-3.5-lightning-30b-a3b", "nvidia/Nemotron-3.5", "glm-5.2", "glm-5.3"} {
		require.False(t, chatModelHasNativeVision(model), model)
	}
}

func TestChatNonVisionImagesUseIndependentSingleImageToolsBeforeTextModel(t *testing.T) {
	db, mock, err := sqlmock.New()
	require.NoError(t, err)
	defer db.Close()
	images := []chatImage{{AssetID: uuid.NewString(), Name: "first.png"}, {AssetID: uuid.NewString(), Name: "second.png"}}
	for _, image := range images {
		mock.ExpectQuery("SELECT media_type,content").WithArgs(int64(42), image.AssetID).WillReturnRows(sqlmock.NewRows([]string{"media_type", "content"}).AddRow("image/png", []byte(image.Name)))
	}
	var calls atomic.Int32
	tool := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		require.Equal(t, "/v1/tools/image_recognition", r.URL.Path)
		require.Equal(t, "Bearer user-jwt", r.Header.Get("Authorization"))
		var input struct {
			Prompt string `json:"prompt"`
			Image  string `json:"image"`
		}
		require.NoError(t, json.NewDecoder(r.Body).Decode(&input))
		require.Contains(t, input.Prompt, "比较两张图")
		require.Contains(t, input.Prompt, "本次只识别")
		require.True(t, strings.HasPrefix(input.Image, "data:image/png;base64,"))
		calls.Add(1)
		text := "红色三角形。"
		if strings.HasSuffix(input.Image, base64.StdEncoding.EncodeToString([]byte("second.png"))) {
			text = "蓝色圆形。"
		}
		_ = json.NewEncoder(w).Encode(map[string]any{"name": "image_recognition", "status": "completed", "cached": false, "output": map[string]any{"text": text, "image_count": 1}, "billing": map[string]any{"mode": "per_image", "images": 1}})
	}))
	defer tool.Close()
	model := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		require.EqualValues(t, 2, calls.Load())
		require.Equal(t, "/api/v1/chat/completions", r.URL.Path)
		var payload map[string]any
		require.NoError(t, json.NewDecoder(r.Body).Decode(&payload))
		require.Equal(t, "glm-5.3", payload["model"])
		require.Equal(t, "max", payload["reasoning_effort"])
		messages := payload["messages"].([]any)
		require.Equal(t, "system", messages[0].(map[string]any)["role"])
		require.Equal(t, chatCurrentVisionInstruction, messages[0].(map[string]any)["content"])
		content := messages[1].(map[string]any)["content"]
		require.Equal(t, "比较两张图\n\n[图片识别结果]\n图片 1：\n红色三角形。\n\n图片 2：\n蓝色圆形。", content)
		w.Header().Set("Content-Type", "text/event-stream")
		_, _ = io.WriteString(w, "data: {\"choices\":[{\"delta\":{\"content\":\"形状和颜色都不同\"}}]}\n\ndata: [DONE]\n\n")
	}))
	defer model.Close()
	h := &ChatHistoryHandler{db: db, baseURL: model.URL, toolURL: tool.URL, client: model.Client()}
	out := make(chan chatDelta, 8)
	h.modelStream(context.Background(), 42, chatRecord{Model: "glm-5.3", Messages: []chatMessage{{Role: "user", Content: "比较两张图", Images: images}, {Role: "assistant"}}}, "Bearer user-jwt", map[string]json.RawMessage{"reasoning_effort": json.RawMessage(`"max"`)}, out)
	var tools []service.InternalToolEvent
	for range 4 {
		delta := <-out
		require.NotNil(t, delta.Tool)
		tools = applyChatToolEvent(tools, *delta.Tool)
	}
	require.Len(t, tools, 2)
	for _, completed := range tools {
		require.Equal(t, "completed", completed.Status)
		require.Equal(t, "image_recognition", completed.Name)
		require.False(t, completed.Cached)
		require.NotContains(t, string(completed.Input), "base64")
		var input chatImageToolInput
		require.NoError(t, json.Unmarshal(completed.Input, &input))
		require.Len(t, input.Images, 1)
	}
	require.Equal(t, "形状和颜色都不同", (<-out).Content)
	require.True(t, (<-out).Done)
	require.NoError(t, mock.ExpectationsWereMet())
}

func TestChatImageToolFailureNeverSendsImagesOrStartsTextModel(t *testing.T) {
	for _, tc := range []struct {
		name   string
		status int
		body   string
	}{
		{"capacity", 429, `{"error":{"message":"识图工具并发已满，请稍后重试"}}`},
		{"auth", 401, `{"error":{"message":"请先登录"}}`},
		{"empty", 200, ""},
		{"malformed", 200, `{"name":"image_recognition","status":"completed","output":{"text":"","image_count":1}}`},
		{"wrong count", 200, `{"name":"image_recognition","status":"completed","output":{"text":"描述","image_count":2}}`},
	} {
		t.Run(tc.name, func(t *testing.T) {
			db, mock, err := sqlmock.New()
			require.NoError(t, err)
			defer db.Close()
			asset := uuid.NewString()
			mock.ExpectQuery("SELECT media_type,content").WithArgs(int64(42), asset).WillReturnRows(sqlmock.NewRows([]string{"media_type", "content"}).AddRow("image/png", []byte("image")))
			var calls atomic.Int32
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				require.Equal(t, "/v1/tools/image_recognition", r.URL.Path)
				calls.Add(1)
				w.WriteHeader(tc.status)
				_, _ = io.WriteString(w, tc.body)
			}))
			defer server.Close()
			h := &ChatHistoryHandler{db: db, baseURL: server.URL, toolURL: server.URL, client: server.Client()}
			out := make(chan chatDelta, 4)
			h.modelStream(context.Background(), 42, chatRecord{Model: "Nemotron-3.5", Messages: []chatMessage{{Role: "user", Images: []chatImage{{AssetID: asset}}}, {Role: "assistant"}}}, "Bearer user-jwt", nil, out)
			require.Equal(t, "running", (<-out).Tool.Status)
			require.Equal(t, "failed", (<-out).Tool.Status)
			require.Error(t, (<-out).Err)
			require.EqualValues(t, 1, calls.Load(), "paid requests are never automatically replayed")
			require.NoError(t, mock.ExpectationsWereMet())
		})
	}
}

func TestChatImageToolHistorySurvivesModelSwitchWithoutReadingOrChargingAgain(t *testing.T) {
	db, mock, err := sqlmock.New()
	require.NoError(t, err)
	defer db.Close()
	image := chatImage{AssetID: uuid.NewString()}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		require.Equal(t, "/api/v1/chat/completions", r.URL.Path)
		raw, _ := io.ReadAll(r.Body)
		require.NotContains(t, string(raw), "image_url")
		require.Contains(t, string(raw), "红色三角形")
		w.Header().Set("Content-Type", "text/event-stream")
		_, _ = io.WriteString(w, "data: [DONE]\n\n")
	}))
	defer server.Close()
	h := &ChatHistoryHandler{db: db, baseURL: server.URL, toolURL: server.URL, client: server.Client()}
	item := chatRecord{Model: "glm-5.2", Messages: []chatMessage{
		{Role: "user", Images: []chatImage{image}}, {Role: "assistant", Content: "第一次使用原生识图"},
		{Role: "user", Content: "换了文字模型以后呢"}, {Role: "assistant", Tools: []service.InternalToolEvent{{Type: "tool_call", ID: "old-image", Name: "image_recognition", Status: "completed", Output: "红色三角形", Input: mustChatJSON(chatImageToolInput{Images: []chatImage{image}})}}},
		{Role: "user", Content: "继续"}, {Role: "assistant"},
	}}
	out := make(chan chatDelta, 2)
	h.modelStream(context.Background(), 42, item, "Bearer user-jwt", nil, out)
	require.True(t, (<-out).Done)
	require.NoError(t, mock.ExpectationsWereMet())
}

func TestChatImageToolCancellationStopsThePendingRequest(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	entered := make(chan struct{})
	release := make(chan struct{})
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		close(entered)
		<-release
	}))
	defer server.Close()
	h := &ChatHistoryHandler{toolURL: server.URL, client: server.Client()}
	done := make(chan error, 1)
	go func() {
		_, err := h.recognizeChatImages(ctx, []chatImage{{AssetID: uuid.NewString()}}, []string{"data:image/png;base64,AAAA"}, "", "Bearer user-jwt", func(chatDelta) bool { return true })
		done <- err
	}()
	<-entered
	cancel()
	select {
	case err := <-done:
		require.Error(t, err)
	case <-time.After(2 * time.Second):
		t.Fatal("cancellation did not stop the tool request")
	}
	close(release)
}

func TestChatImageToolFollowupDoesNotRetryFailedOrStoppedHistoricalRecognition(t *testing.T) {
	for _, previousError := range []string{"识图工具并发已满，请稍后重试", "已停止生成"} {
		t.Run(previousError, func(t *testing.T) {
			db, mock, err := sqlmock.New()
			require.NoError(t, err)
			defer db.Close()
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				require.Equal(t, "/api/v1/chat/completions", r.URL.Path)
				raw, _ := io.ReadAll(r.Body)
				require.NotContains(t, string(raw), "image_url")
				require.Contains(t, string(raw), "图片识别未完成")
				w.Header().Set("Content-Type", "text/event-stream")
				_, _ = io.WriteString(w, "data: [DONE]\n\n")
			}))
			defer server.Close()
			h := &ChatHistoryHandler{db: db, baseURL: server.URL, toolURL: server.URL, client: server.Client()}
			item := chatRecord{Model: "Nemotron-3.5", Messages: []chatMessage{
				{Role: "user", Images: []chatImage{{AssetID: uuid.NewString()}}},
				{Role: "assistant", Error: previousError},
				{Role: "user", Content: "那先帮我写代码"}, {Role: "assistant"},
			}}
			out := make(chan chatDelta, 2)
			h.modelStream(context.Background(), 42, item, "Bearer user-jwt", nil, out)
			require.True(t, (<-out).Done)
			require.NoError(t, mock.ExpectationsWereMet())
		})
	}
}
