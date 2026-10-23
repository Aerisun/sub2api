package handler

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"

	"github.com/Wei-Shaw/sub2api/internal/service"
)

type chatModelFunction struct {
	Name      string `json:"name"`
	Arguments string `json:"arguments"`
}
type chatModelCall struct {
	ID       string            `json:"id"`
	Type     string            `json:"type"`
	Function chatModelFunction `json:"function"`
}
type chatModelRound struct {
	Content, Reasoning string
	Calls              []chatModelCall
}

func (h *ChatHistoryHandler) chatModelRound(ctx context.Context, item chatRecord, payload map[string]any, auth string, round int, emit func(chatDelta) bool) (chatModelRound, error) {
	result := chatModelRound{}
	raw, err := json.Marshal(payload)
	if err != nil {
		return result, err
	}
	request, err := http.NewRequestWithContext(ctx, http.MethodPost, h.baseURL+"/api/v1/chat/completions", bytes.NewReader(raw))
	if err != nil {
		return result, err
	}
	request.Header.Set("Authorization", auth)
	request.Header.Set("Content-Type", "application/json")
	identity := "chat-generation-" + item.Messages[len(item.Messages)-1].ID
	if round > 0 {
		identity += fmt.Sprintf("-vision-%d", round)
	}
	request.Header.Set("Idempotency-Key", identity)
	if item.ID != "" {
		request.Header.Set("X-Session-Id", "chat_"+item.ID)
	}
	response, err := h.client.Do(request)
	if err != nil {
		return result, err
	}
	defer func() { _ = response.Body.Close() }()
	if response.StatusCode != http.StatusOK {
		raw, _ := io.ReadAll(io.LimitReader(response.Body, 8192))
		var failure struct {
			Message string `json:"message"`
			Error   struct {
				Message string `json:"message"`
			} `json:"error"`
		}
		_ = json.Unmarshal(raw, &failure)
		message := failure.Error.Message
		if message == "" {
			message = failure.Message
		}
		if message == "" {
			message = fmt.Sprintf("模型请求失败（%d）", response.StatusCode)
		}
		return result, errors.New(message)
	}
	scanner := bufio.NewScanner(response.Body)
	scanner.Buffer(make([]byte, 4096), 8*1024*1024)
	var content, reasoning strings.Builder
	calls := make(map[int]*chatModelCall)
	argumentBytes := 0
	for scanner.Scan() {
		if !strings.HasPrefix(scanner.Text(), "data:") {
			continue
		}
		data := strings.TrimSpace(strings.TrimPrefix(scanner.Text(), "data:"))
		if data == "[DONE]" {
			result.Content, result.Reasoning = content.String(), reasoning.String()
			ids := make(map[string]bool)
			for index := 0; index < 8; index++ {
				if call := calls[index]; call != nil {
					if call.ID == "" || call.Function.Name == "" || ids[call.ID] {
						return result, errors.New("模型返回的工具调用不完整")
					}
					ids[call.ID] = true
					result.Calls = append(result.Calls, *call)
				}
			}
			return result, nil
		}
		var tool service.InternalToolEvent
		if json.Unmarshal([]byte(data), &tool) == nil && tool.Type == "tool_call" && validChatToolEvent(tool) {
			if !emit(chatDelta{Tool: &tool}) {
				return result, ctx.Err()
			}
			continue
		}
		var event struct {
			Error *struct {
				Message string `json:"message"`
			} `json:"error"`
			Choices []struct {
				Index int `json:"index"`
				Delta struct {
					Content          string `json:"content"`
					Reasoning        string `json:"reasoning"`
					ReasoningContent string `json:"reasoning_content"`
					ToolCalls        []struct {
						Index    int               `json:"index"`
						ID       string            `json:"id"`
						Type     string            `json:"type"`
						Function chatModelFunction `json:"function"`
					} `json:"tool_calls"`
				} `json:"delta"`
			} `json:"choices"`
		}
		if json.Unmarshal([]byte(data), &event) != nil {
			continue
		}
		if event.Error != nil {
			return result, errors.New(event.Error.Message)
		}
		for _, choice := range event.Choices {
			if choice.Index != 0 {
				continue
			}
			delta := choice.Delta
			thought := delta.ReasoningContent
			if thought == "" {
				thought = delta.Reasoning
			}
			if content.Len()+reasoning.Len()+len(delta.Content)+len(thought) > 8*1024*1024 {
				return result, errors.New("回复已达到长度上限")
			}
			_, _ = content.WriteString(delta.Content)
			_, _ = reasoning.WriteString(thought)
			if delta.Content != "" || thought != "" {
				if !emit(chatDelta{Content: delta.Content, Reasoning: thought}) {
					return result, ctx.Err()
				}
			}
			for _, fragment := range delta.ToolCalls {
				if fragment.Index < 0 || fragment.Index >= 8 || (fragment.Type != "" && fragment.Type != "function") {
					return result, errors.New("模型返回的工具调用格式无效")
				}
				call := calls[fragment.Index]
				if call == nil {
					call = &chatModelCall{Type: "function"}
					calls[fragment.Index] = call
				}
				if fragment.ID != call.ID {
					call.ID += fragment.ID
				}
				if fragment.Function.Name != call.Function.Name {
					call.Function.Name += fragment.Function.Name
				}
				call.Function.Arguments += fragment.Function.Arguments
				argumentBytes += len(fragment.Function.Arguments)
				if len(call.ID) > 200 || len(call.Function.Name) > 100 || argumentBytes > 64*1024 {
					return result, errors.New("模型返回的工具参数过大")
				}
			}
		}
	}
	if err := scanner.Err(); err != nil {
		return result, err
	}
	return result, errors.New("模型连接中断，可重新生成")
}
