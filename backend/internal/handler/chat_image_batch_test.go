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
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/DATA-DOG/go-sqlmock"
	"github.com/Wei-Shaw/sub2api/internal/service"
	"github.com/google/uuid"
	"github.com/stretchr/testify/require"
)

func TestChatBatchTranscriptImageLimit(t *testing.T) {
	images := make([]chatImage, 13)
	for i := range images {
		images[i] = chatImage{ClientID: uuid.NewString(), Name: fmt.Sprintf("%d.png", i), MediaType: "image/png"}
	}
	item := chatRecord{Title: "Batch fixture", Model: "Nemotron-3.5", Messages: []chatMessage{{Role: "user", Images: images[:12]}, {Role: "assistant"}}}
	require.True(t, validChatRecord(&item))
	item.Messages[0].Images = images
	require.False(t, validChatRecord(&item))
}

func TestChatHistoricalSelectionIsExplicitUniqueAndConversationBound(t *testing.T) {
	first := chatImage{ClientID: uuid.NewString(), Name: "first.png", MediaType: "image/png"}
	second := chatImage{AssetID: uuid.NewString(), Name: "second.png", MediaType: "image/png"}
	current := chatImage{ClientID: uuid.NewString(), Name: "current.png", MediaType: "image/png"}
	messages := []chatMessage{{Role: "user", Images: []chatImage{first, second}}, {Role: "assistant"}, {Role: "user", Images: []chatImage{current}}, {Role: "assistant"}}
	selected, err := chatSelectedHistoryImages(messages)
	require.NoError(t, err)
	require.Empty(t, selected)
	messages[2].RecognitionImages = []string{chatImageKey(second), chatImageKey(first)}
	selected, err = chatSelectedHistoryImages(messages)
	require.NoError(t, err)
	require.Equal(t, []chatImage{second, first}, selected)
	for _, keys := range [][]string{{chatImageKey(first), chatImageKey(first)}, {chatImageKey(current)}, {"browser:" + uuid.NewString()}} {
		messages[2].RecognitionImages = keys
		_, err = chatSelectedHistoryImages(messages)
		require.Error(t, err)
	}
	messages[2].RecognitionImages = []string{chatImageKey(first)}
	data := "data:image/png;base64," + base64.StdEncoding.EncodeToString([]byte("\x89PNG\r\n\x1a\nfixture"))
	require.NoError(t, validateChatImageInputs([]chatImage{first, current}, map[string]string{first.ClientID: data, current.ClientID: data}))
	require.Error(t, validateChatImageInputs([]chatImage{first, current}, map[string]string{current.ClientID: data}))
	require.Error(t, validateChatImageInputs([]chatImage{first, current}, map[string]string{first.ClientID: data, current.ClientID: data, uuid.NewString(): data}))
}

func TestChatTwelveIndependentImageCallsStreamTogetherAndRetainOrder(t *testing.T) {
	images := make([]chatImage, 12)
	urls := make([]string, 12)
	for i := range images {
		images[i] = chatImage{ClientID: uuid.NewString(), Name: fmt.Sprintf("%02d.png", i)}
		urls[i] = images[i].Name
	}
	partials := make(chan service.InternalToolEvent, 12)
	release := make(chan struct{})
	var releaseOnce sync.Once
	unblock := func() { releaseOnce.Do(func() { close(release) }) }
	var calls atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var input struct {
			Image  string   `json:"image"`
			Prompt string   `json:"prompt"`
			Images []string `json:"images"`
		}
		if json.NewDecoder(r.Body).Decode(&input) != nil || input.Image == "" || input.Images != nil {
			http.Error(w, "one image required", 400)
			return
		}
		calls.Add(1)
		w.Header().Set("Content-Type", "text/event-stream")
		progress := map[string]any{"type": "tool_call", "id": "remote-" + input.Image, "name": "image_recognition", "status": "running", "delta": input.Image}
		raw, _ := json.Marshal(progress)
		fmt.Fprintf(w, "event: aerisun.tool\ndata: %s\n\n", raw)
		w.(http.Flusher).Flush()
		select {
		case <-release:
		case <-r.Context().Done():
			return
		}
		result, _ := json.Marshal(map[string]any{"name": "image_recognition", "status": "completed", "output": map[string]any{"text": input.Image, "image_count": 1}})
		fmt.Fprintf(w, "event: result\ndata: %s\n\n", result)
	}))
	defer server.Close()
	defer unblock()
	h := &ChatHistoryHandler{toolURL: server.URL, client: server.Client()}
	var mutex sync.Mutex
	var events []service.InternalToolEvent
	type outcome struct {
		text string
		err  error
	}
	done := make(chan outcome, 1)
	go func() {
		text, err := h.recognizeChatImageBatches(context.Background(), images, urls, "介绍这些图片", "Bearer fixture", func(delta chatDelta) bool {
			mutex.Lock()
			events = append(events, *delta.Tool)
			mutex.Unlock()
			if delta.Tool.Delta != "" {
				partials <- *delta.Tool
			}
			return true
		})
		done <- outcome{text, err}
	}()
	seen := make(map[string]bool)
	for range images {
		select {
		case event := <-partials:
			require.False(t, seen[event.ID])
			seen[event.ID] = true
		case <-time.After(2 * time.Second):
			t.Fatal("one-image calls did not stream concurrently before any final result")
		}
	}
	select {
	case <-done:
		t.Fatal("batch finished before all independent streams completed")
	default:
	}
	unblock()
	var final outcome
	select {
	case final = <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("batch did not finish")
	}
	require.NoError(t, final.err)
	for i := 1; i < len(urls); i++ {
		require.Less(t, strings.Index(final.text, urls[i-1]), strings.Index(final.text, urls[i]))
	}
	require.EqualValues(t, 12, calls.Load())
	require.Len(t, events, 36)
	completed := make(map[string]bool)
	for _, event := range events {
		var input chatImageToolInput
		require.NoError(t, json.Unmarshal(event.Input, &input))
		require.Equal(t, 12, input.BatchCount)
		require.Equal(t, input.BatchIndex, input.ImageStart)
		require.Len(t, input.Images, 1)
		require.Equal(t, images[input.BatchIndex-1].ClientID, input.Images[0].ClientID)
		if event.Status == "completed" {
			require.Equal(t, urls[input.BatchIndex-1], event.Output)
			completed[event.ID] = true
		}
	}
	require.Len(t, completed, 12)
	_, err := h.recognizeChatImageBatches(context.Background(), append(images, chatImage{}), append(urls, "extra"), "", "", func(chatDelta) bool { return true })
	require.Error(t, err)
	_, err = h.recognizeChatImages(context.Background(), images[:2], urls[:2], "", "", func(chatDelta) bool { t.Error("invalid multi-image tool emitted an event"); return true })
	require.Error(t, err)
	require.EqualValues(t, 12, calls.Load())
}

func TestChatLegacyHistorySelectionDoesNotForceAPaidRecognition(t *testing.T) {
	db, mock, err := sqlmock.New()
	require.NoError(t, err)
	defer db.Close()
	selected := chatImage{ClientID: uuid.NewString(), Name: "selected.png"}
	other := chatImage{ClientID: uuid.NewString(), Name: "other.png"}
	var calls atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/v1/tools/image_recognition" {
			calls.Add(1)
			var input struct {
				Image string `json:"image"`
			}
			require.NoError(t, json.NewDecoder(r.Body).Decode(&input))
			require.Equal(t, "selected-original", input.Image)
			_, _ = io.WriteString(w, `{"name":"image_recognition","status":"completed","output":{"text":"选中图片的新结果","image_count":1}}`)
			return
		}
		require.EqualValues(t, 0, calls.Load())
		body, _ := io.ReadAll(r.Body)
		require.Contains(t, string(body), "另一张的旧结果")
		require.NotContains(t, string(body), "选中图片的新结果")
		require.NotContains(t, string(body), "selected-original")
		require.NotContains(t, string(body), "image_url")
		w.Header().Set("Content-Type", "text/event-stream")
		_, _ = io.WriteString(w, "data: [DONE]\n\n")
	}))
	defer server.Close()
	h := &ChatHistoryHandler{db: db, baseURL: server.URL, toolURL: server.URL, client: server.Client()}
	item := chatRecord{Model: "Nemotron-3.5", Messages: []chatMessage{{Role: "user", Images: []chatImage{selected, other}},
		{Role: "assistant", Tools: []service.InternalToolEvent{{Name: "image_recognition", Status: "completed", Output: "另一张的旧结果", Input: mustChatJSON(chatImageToolInput{Images: []chatImage{other}})}}},
		{Role: "user", RecognitionImages: []string{chatImageKey(selected)}}, {Role: "assistant"}}}
	ctx := context.WithValue(context.Background(), chatImageInputsKey{}, map[string]string{selected.ClientID: "selected-original"})
	out := make(chan chatDelta, 8)
	h.modelStream(ctx, 42, item, "Bearer fixture", nil, out)
	require.True(t, (<-out).Done)
	require.NoError(t, mock.ExpectationsWereMet())
}

func TestChatBatchFailureCancelsOtherRequestsWithoutRetry(t *testing.T) {
	entered := make(chan struct{}, 12)
	release := make(chan struct{})
	var calls atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		var input struct {
			Image string `json:"image"`
		}
		_ = json.NewDecoder(r.Body).Decode(&input)
		entered <- struct{}{}
		<-release
		if input.Image == "0" {
			w.WriteHeader(429)
			_, _ = io.WriteString(w, `{"error":{"message":"fixture capacity"}}`)
			return
		}
		select {
		case <-r.Context().Done():
		case <-time.After(2 * time.Second):
		}
	}))
	defer server.Close()
	images := make([]chatImage, 12)
	urls := make([]string, 12)
	for i := range images {
		images[i].ClientID = uuid.NewString()
		urls[i] = fmt.Sprint(i)
	}
	h := &ChatHistoryHandler{toolURL: server.URL, client: server.Client()}
	done := make(chan error, 1)
	go func() {
		_, err := h.recognizeChatImageBatches(context.Background(), images, urls, "", "Bearer fixture", func(chatDelta) bool { return true })
		done <- err
	}()
	for i := 0; i < 12; i++ {
		select {
		case <-entered:
		case <-time.After(2 * time.Second):
			close(release)
			t.Fatal("parallel calls did not start")
		}
	}
	close(release)
	select {
	case err := <-done:
		require.ErrorContains(t, err, "fixture capacity")
	case <-time.After(time.Second):
		t.Fatal("failed batch did not cancel sibling calls")
	}
	require.EqualValues(t, 12, calls.Load())
}
