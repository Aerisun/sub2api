package handler

import (
	"encoding/json"
	"strings"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/service"
	"github.com/stretchr/testify/require"
)

func TestChatWebSearchProgressSourcesPersistWithOffsets(t *testing.T) {
	event := service.InternalToolEvent{Type: "tool_call", ID: "search-1", Name: "web_search", Status: "running", ContentOffset: 5, ReasoningOffset: 8,
		Input: json.RawMessage(`{"queries":[{"query":"文档"}]}`)}
	require.True(t, validChatToolEvent(event))
	tools := applyChatToolEvent(nil, event)
	completed := service.InternalToolEvent{Type: "tool_call", ID: "search-1", Name: "web_search", Status: "completed", Output: "真实摘要",
		Metadata: json.RawMessage(`{"mode":"live","searched_at":"2026-10-21T02:00:00Z","returned_sources":1}`),
		Sources:  []service.InternalToolSource{{ID: "source-1", Title: "官方文档", URL: "https://example.com/docs#section", Snippet: "来源摘要", Content: "完整来源摘录", ReferenceIDs: []string{"source-1"}, Published: "2026-10-20", Crawled: "today"}}}
	require.True(t, validChatToolEvent(completed))
	tools = applyChatToolEvent(tools, completed)
	require.JSONEq(t, string(event.Input), string(tools[0].Input))
	require.Equal(t, 5, tools[0].ContentOffset)
	require.Equal(t, 8, tools[0].ReasoningOffset)
	encoded, err := json.Marshal(tools)
	require.NoError(t, err)
	var restored []service.InternalToolEvent
	require.NoError(t, json.Unmarshal(encoded, &restored))
	require.Equal(t, tools, restored)
	item := chatRecord{Title: "搜索", Model: "qwen3.8-flash", Messages: []chatMessage{{Role: "assistant", Tools: restored}}}
	require.True(t, validChatRecord(&item))
	// Subsequent status-only updates retain query, sources and original position.
	tools = applyChatToolEvent(tools, service.InternalToolEvent{Type: "tool_call", ID: "search-1", Name: "web_search", Status: "completed"})
	require.Len(t, tools[0].Sources, 1)
	require.Equal(t, completed.Metadata, tools[0].Metadata)
	require.Equal(t, "完整来源摘录", tools[0].Sources[0].Content)
	// An ID cannot switch its tool identity midway through a stream.
	tools = applyChatToolEvent(tools, service.InternalToolEvent{Type: "tool_call", ID: "search-1", Name: "image_generation", Status: "failed"})
	require.Equal(t, "web_search", tools[0].Name)
}

func TestChatWebSearchRejectsUnsafeAndUnboundedToolOutput(t *testing.T) {
	base := service.InternalToolEvent{Type: "tool_call", ID: "search-1", Name: "web_search", Status: "completed"}
	for _, link := range []string{"javascript:alert(1)", "data:text/html,body", "https://user:secret@example.com", "https:///no-host"} {
		event := base
		event.Sources = []service.InternalToolSource{{URL: link}}
		require.False(t, validChatToolEvent(event), link)
	}
	event := base
	event.Input = json.RawMessage(`not-json`)
	require.False(t, validChatToolEvent(event))
	event.Input = json.RawMessage(`"` + strings.Repeat("x", 32*1024) + `"`)
	require.False(t, validChatToolEvent(event))
	event = base
	event.Sources = make([]service.InternalToolSource, 51)
	require.False(t, validChatToolEvent(event))
	for _, value := range []string{`null`, `[]`, `"text"`, `invalid`} {
		event = base
		event.Metadata = json.RawMessage(value)
		require.False(t, validChatToolEvent(event))
	}
	event = base
	for i := 0; i < 20; i++ {
		event.Sources = append(event.Sources, service.InternalToolSource{ID: "id", URL: "https://example.com/docs", Content: strings.Repeat("x", 6000)})
	}
	require.True(t, validChatToolEvent(event))
	event.Sources[0].Content = strings.Repeat("x", 32769)
	require.False(t, validChatToolEvent(event))
	event.Sources[0].Content = strings.Repeat("x", 32768)
	require.False(t, validChatToolEvent(event)) // Total source content is bounded too.
}
