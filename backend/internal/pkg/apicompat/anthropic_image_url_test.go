package apicompat

import (
	"encoding/json"
	"testing"

	"github.com/stretchr/testify/require"
)

func TestAnthropicImageURLSurvivesChatAndResponsesConversion(t *testing.T) {
	const imageURL = "https://images.example/test.png?version=2"
	for _, toolResult := range []bool{false, true} {
		name := "user_image"
		content := `[{"type":"text","text":"Read the actual image"},{"type":"image","source":{"type":"url","url":"` + imageURL + `"}}]`
		var messages []AnthropicMessage
		if toolResult {
			name = "tool_result_image"
			messages = append(messages, AnthropicMessage{Role: "assistant", Content: json.RawMessage(`[{"type":"tool_use","id":"toolu_read","name":"Read","input":{"path":"test.png"}}]`)})
			content = `[{"type":"tool_result","tool_use_id":"toolu_read","content":` + content + `}]`
		}
		messages = append(messages, AnthropicMessage{Role: "user", Content: json.RawMessage(content)})
		t.Run(name, func(t *testing.T) {
			req := &AnthropicRequest{Model: "qwen3.8-flash", Messages: messages}
			chat, err := AnthropicToChatCompletionsRequest(req)
			require.NoError(t, err)
			chatJSON, err := json.Marshal(chat)
			require.NoError(t, err)
			require.Contains(t, string(chatJSON), `"type":"image_url"`)
			require.Contains(t, string(chatJSON), imageURL)
			responses, err := AnthropicToResponses(req)
			require.NoError(t, err)
			require.Contains(t, string(responses.Input), `"type":"input_image"`)
			require.Contains(t, string(responses.Input), imageURL)
		})
	}
}

func TestAnthropicImageURLSourceRequiresHTTPURL(t *testing.T) {
	for _, value := range []string{"file:///tmp/image.png", "javascript:alert(1)", "https:///missing-host", ""} {
		require.Empty(t, anthropicImageToDataURI(&AnthropicImageSource{Type: "url", URL: value}))
	}
}
