package handler

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/service"
	"github.com/google/uuid"
	"github.com/stretchr/testify/require"
)

func TestChatRecognitionStreamsRealDeltasBeforeFinalResult(t *testing.T) {
	release := make(chan struct{})
	partial := make(chan service.InternalToolEvent, 8)
	var requests atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		requests.Add(1)
		require.Equal(t, "text/event-stream", r.Header.Get("Accept"))
		var body map[string]any
		require.NoError(t, json.NewDecoder(r.Body).Decode(&body))
		require.NotEmpty(t, body["image"])
		require.NotContains(t, body, "images")
		w.Header().Set("Content-Type", "text/event-stream")
		fmt.Fprint(w, "event: aerisun.tool\r\ndata: {\"type\":\"tool_call\",\"id\":\"remote\",\"name\":\"image_recognition\",\"status\":\"running\",\"delta\":\"红色\"}\r\n\r\n")
		flusher, ok := w.(http.Flusher)
		require.True(t, ok)
		flusher.Flush()
		select {
		case <-release:
		case <-r.Context().Done():
			return
		}
		fmt.Fprint(w, "event: aerisun.tool\ndata: {\"type\":\"tool_call\",\"id\":\"remote\",\"name\":\"image_recognition\",\"status\":\"running\",\"delta\":\"方块\"}\n\n")
		fmt.Fprint(w, "event: result\ndata: {\"name\":\"image_recognition\",\"status\":\"completed\",\"output\":{\"text\":\"红色方块\",\"image_count\":1}}\n\n")
	}))
	defer server.Close()
	h := &ChatHistoryHandler{toolURL: server.URL, client: server.Client()}
	done := make(chan error, 1)
	go func() {
		text, err := h.recognizeChatImages(context.Background(), []chatImage{{ClientID: uuid.NewString(), Name: "local.png", MediaType: "image/png"}}, []string{"data:image/png;base64,fixture"}, "描述", "Bearer fixture", func(d chatDelta) bool {
			if d.Tool != nil {
				partial <- *d.Tool
			}
			return true
		})
		if err == nil && text != "红色方块" {
			err = fmt.Errorf("wrong final text")
		}
		done <- err
	}()
	initial := <-partial
	require.Equal(t, "running", initial.Status)
	var first service.InternalToolEvent
	select {
	case first = <-partial:
	case <-time.After(2 * time.Second):
		close(release)
		t.Fatal("real text was buffered until completion")
	}
	require.Equal(t, "红色", first.Delta)
	require.Equal(t, initial.ID, first.ID)
	select {
	case completionErr := <-done:
		t.Logf("server shutdown completed: %v", completionErr)
		close(release)
		t.Fatal("recognition finished before fixture released")
	default:
	}
	close(release)
	require.NoError(t, <-done)
	second, final := <-partial, <-partial
	require.Equal(t, "方块", second.Delta)
	require.Equal(t, "completed", final.Status)
	require.Equal(t, "红色方块", final.Output)
	tools := applyChatToolEvent(nil, initial)
	tools = applyChatToolEvent(tools, first)
	tools = applyChatToolEvent(tools, second)
	tools = applyChatToolEvent(tools, final)
	require.Equal(t, "红色方块", tools[0].Output, "final result must replace, not duplicate streamed text")
	require.EqualValues(t, 1, requests.Load())
}

func TestChatRecognitionInterruptedOrInconsistentStreamKeepsPartialAsFailed(t *testing.T) {
	for _, tail := range []string{"", "event: error\ndata: {\"error\":{\"message\":\"上游连接中断\"}}\n\n", "event: result\ndata: {\"name\":\"image_recognition\",\"status\":\"completed\",\"output\":{\"text\":\"不同的内容\",\"image_count\":1}}\n\n"} {
		t.Run(fmt.Sprint(len(tail)), func(t *testing.T) {
			var requests atomic.Int32
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				requests.Add(1)
				w.Header().Set("Content-Type", "text/event-stream")
				fmt.Fprint(w, "event: aerisun.tool\ndata: {\"type\":\"tool_call\",\"id\":\"one\",\"name\":\"image_recognition\",\"status\":\"running\",\"delta\":\"已识别部分\"}\n\n"+tail)
			}))
			defer server.Close()
			h := &ChatHistoryHandler{toolURL: server.URL, client: server.Client()}
			var tools []service.InternalToolEvent
			_, err := h.recognizeChatImages(context.Background(), []chatImage{{ClientID: uuid.NewString()}}, []string{"fixture"}, "", "Bearer fixture", func(d chatDelta) bool { tools = applyChatToolEvent(tools, *d.Tool); return true })
			require.Error(t, err)
			require.Len(t, tools, 1)
			require.Equal(t, "failed", tools[0].Status)
			require.Equal(t, "已识别部分", tools[0].Output)
			require.EqualValues(t, 1, requests.Load())
		})
	}
}
