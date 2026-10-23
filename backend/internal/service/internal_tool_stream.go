package service

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"

	"github.com/gin-gonic/gin"
	"github.com/tidwall/gjson"
)

// Internal tools are server-executed. Their separate SSE event must never be
// advertised as a client tool_use/tool_calls operation that the SDK should run.
const InternalToolEventName = "aerisun.tool"

type InternalToolSource struct {
	ID               string   `json:"id"`
	Title            string   `json:"title"`
	URL              string   `json:"url"`
	Snippet          string   `json:"snippet,omitempty"`
	PublishedAt      string   `json:"published_at,omitempty"`
	Domain           string   `json:"domain,omitempty"`
	ReferenceIDs     []string `json:"reference_ids,omitempty"`
	SourceType       string   `json:"type,omitempty"`
	Content          string   `json:"content,omitempty"`
	ContentTruncated bool     `json:"content_truncated,omitempty"`
	Published        string   `json:"published,omitempty"`
	Crawled          string   `json:"crawled,omitempty"`
}

type InternalToolEvent struct {
	Type            string               `json:"type"`
	ID              string               `json:"id"`
	Name            string               `json:"name"`
	Status          string               `json:"status"`
	ContentOffset   int                  `json:"contentOffset,omitempty"`
	ReasoningOffset int                  `json:"reasoningOffset,omitempty"`
	Delta           string               `json:"delta,omitempty"`
	Output          string               `json:"output,omitempty"`
	Cached          bool                 `json:"cached,omitempty"`
	Error           string               `json:"error,omitempty"`
	Input           json.RawMessage      `json:"input,omitempty"`
	Sources         []InternalToolSource `json:"sources,omitempty"`
	Metadata        json.RawMessage      `json:"metadata,omitempty"`
}
type glmVisionProgressKey struct{}

func glmVisionProgress(ctx context.Context) func(string) {
	fn, _ := ctx.Value(glmVisionProgressKey{}).(func(string))
	return fn
}

func writeInternalVisionToolEvent(c *gin.Context, event InternalToolEvent) {
	if c == nil {
		return
	}
	value, exists := c.Get(openAICompactSSEKeepaliveKey)
	k, ok := value.(*openAICompactSSEKeepalive)
	if !exists || !ok || k == nil {
		return
	}
	event.Type = "tool_call"
	raw, err := json.Marshal(event)
	if err != nil {
		return
	}
	k.mu.Lock()
	defer k.mu.Unlock()
	if k.stopped {
		return
	}
	if !k.started {
		header := k.writer.Header()
		header.Set("Content-Type", "text/event-stream")
		header.Set("Cache-Control", "no-cache")
		header.Set("X-Accel-Buffering", "no")
		k.writer.WriteHeader(http.StatusOK)
		k.started = true
	}
	n, err := fmt.Fprintf(k.writer, "event: %s\ndata: %s\n\n", InternalToolEventName, raw)
	// Progress cannot suppress provider failover or become billable text tokens.
	k.bytes += n
	if err != nil {
		k.markStoppedLocked()
		return
	}
	k.writer.Flush()
}

// Consume real upstream deltas. Never simulate streaming by splitting a final
// description; an incomplete result or missing usage must not enter the cache.
func readGLMVisionStream(source io.Reader, emit func(string)) ([]byte, error) {
	scanner := bufio.NewScanner(source)
	scanner.Buffer(make([]byte, 4096), 2*1024*1024)
	var text strings.Builder
	var usage json.RawMessage
	finish, done := "", false
	total := 0
	for scanner.Scan() {
		line := scanner.Text()
		total += len(line)
		if total > 4*1024*1024 {
			return nil, fmt.Errorf("vision stream is too large")
		}
		if !strings.HasPrefix(line, "data:") {
			continue
		}
		data := strings.TrimSpace(strings.TrimPrefix(line, "data:"))
		if data == "[DONE]" {
			done = true
			break
		}
		if !gjson.Valid(data) || gjson.Get(data, "error").Exists() {
			return nil, fmt.Errorf("invalid vision stream")
		}
		part := gjson.Get(data, "choices.0.delta.content").String()
		if part != "" {
			_, _ = text.WriteString(part)
			if emit != nil {
				emit(part)
			}
		}
		if v := gjson.Get(data, "choices.0.finish_reason"); v.Type == gjson.String {
			finish = v.String()
		}
		if v := gjson.Get(data, "usage"); v.IsObject() {
			usage = json.RawMessage(v.Raw)
		}
	}
	if scanner.Err() != nil || !done || finish != "stop" || text.Len() == 0 || usage == nil {
		return nil, fmt.Errorf("incomplete vision stream")
	}
	return json.Marshal(map[string]any{"choices": []any{map[string]any{"message": map[string]string{"content": text.String()}, "finish_reason": finish}}, "usage": usage})
}
