package handler

import (
	"context"
	"encoding/json"
	"strings"

	"github.com/gin-gonic/gin"
	"github.com/tidwall/gjson"
)

type realtimeSearchCall struct {
	id, query string
}

func browserRealtimeSearchCalls(terminal []byte) []realtimeSearchCall {
	if gjson.GetBytes(terminal, "response.status").String() != "completed" {
		return nil
	}
	var calls []realtimeSearchCall
	for _, item := range gjson.GetBytes(terminal, "response.output").Array() {
		if item.Get("type").String() != "function_call" || item.Get("name").String() != "web_search" {
			continue
		}
		id, status := item.Get("call_id").String(), item.Get("status").String()
		if id == "" || len(id) > 128 || (status != "" && status != "completed") {
			continue
		}
		args := item.Get("arguments").String()
		calls = append(calls, realtimeSearchCall{id: id, query: strings.TrimSpace(gjson.Get(args, "query").String())})
		if len(calls) == 4 {
			break
		}
	}
	return calls
}

// Search only for calls present in an upstream terminal. The browser never supplies billing identity or tool output.
func (h *OpenAIGatewayHandler) runBrowserSearchCalls(ctx context.Context, c *gin.Context, accountID int64, calls []realtimeSearchCall, allowed bool, send func([]byte) error) []byte {
	var shown json.RawMessage
	for _, call := range calls {
		if ctx.Err() != nil {
			return nil
		}
		output := json.RawMessage(`{"error":"每轮只能执行一次联网搜索。"}`)
		if allowed {
			allowed = false
			result, err := h.browserRealtimeSearch(ctx, c, accountID, call.query)
			if err != nil || len(result) == 0 || len(result) > 16*1024 {
				output = json.RawMessage(`{"error":"本次联网搜索未完成。"}`)
			} else {
				output = result
			}
		}
		if shown == nil {
			shown = output
		}
		msg, _ := json.Marshal(gin.H{"type": "conversation.item.create", "item": gin.H{"type": "function_call_output", "call_id": call.id, "output": string(output)}})
		if ctx.Err() != nil || send(msg) != nil {
			return nil
		}
	}
	msg, _ := json.Marshal(gin.H{"type": "sub2api.search.done", "result": shown})
	return msg
}
