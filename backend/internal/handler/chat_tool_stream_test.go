package handler

import (
	"github.com/Wei-Shaw/sub2api/internal/service"
	"github.com/stretchr/testify/require"
	"testing"
)

func TestChatToolEventsRemainSeparateAndCompletionReplacesDeltas(t *testing.T) {
	event := service.InternalToolEvent{Type: "tool_call", ID: "vision-1", Name: "image_recognition", Status: "running", ContentOffset: 7, ReasoningOffset: 13}
	require.True(t, validChatToolEvent(event))
	tools := applyChatToolEvent(nil, event)
	tools = applyChatToolEvent(tools, service.InternalToolEvent{Type: "tool_call", ID: "vision-1", Name: "image_recognition", Status: "running", Delta: "红色"})
	tools = applyChatToolEvent(tools, service.InternalToolEvent{Type: "tool_call", ID: "vision-1", Name: "image_recognition", Status: "running", Delta: "三角形"})
	require.Equal(t, "红色三角形", tools[0].Output)
	event = service.InternalToolEvent{Type: "tool_call", ID: "vision-1", Name: "image_recognition", Status: "completed", Output: "红色三角形", Cached: true}
	tools = applyChatToolEvent(tools, event)
	require.Len(t, tools, 1)
	require.Equal(t, "红色三角形", tools[0].Output)
	require.Empty(t, tools[0].Delta)
	require.Equal(t, 7, tools[0].ContentOffset)
	require.Equal(t, 13, tools[0].ReasoningOffset)
	item := chatRecord{Title: "image", Model: "Nemotron-3.5", Messages: []chatMessage{{Role: "assistant", Tools: tools}}}
	require.True(t, validChatRecord(&item))
	event.Name = "Read"
	require.False(t, validChatToolEvent(event))
}
