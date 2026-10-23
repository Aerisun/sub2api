package apicompat

import (
	"encoding/json"
	"testing"

	"github.com/stretchr/testify/require"
)

func TestDomesticMessagesPreserveNonToolThinkingHistory(t *testing.T) {
	for _, model := range []string{"deepseek-v4.1-flash", "kimi-k3", "glm-5.3"} {
		t.Run(model, func(t *testing.T) {
			req := &AnthropicRequest{Model: model, MaxTokens: 256,
				Messages: []AnthropicMessage{
					{Role: "user", Content: json.RawMessage(`"first question"`)},
					{Role: "assistant", Content: json.RawMessage(`[{"type":"thinking","thinking":"prior reasoning without a tool"},{"type":"text","text":"first answer"}]`)},
					{Role: "user", Content: json.RawMessage(`"now use a tool"`)},
				},
			}
			out, err := AnthropicToChatCompletionsRequest(req)
			require.NoError(t, err)
			require.Equal(t, "prior reasoning without a tool", out.Messages[1].ReasoningContent)
			raw, err := json.Marshal(out)
			require.NoError(t, err)
			require.Contains(t, string(raw), `"reasoning_content":"prior reasoning without a tool"`)
		})
	}
}
