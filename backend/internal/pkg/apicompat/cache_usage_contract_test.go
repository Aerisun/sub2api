package apicompat

import (
	"encoding/json"
	"testing"

	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
)

func TestCacheUsagePreservesMissAndHitAcrossProtocols(t *testing.T) {
	for _, cached := range []int{0, 30} {
		anthropic := &AnthropicResponse{Usage: AnthropicUsage{
			InputTokens: 100 - cached, OutputTokens: 5, CacheReadInputTokens: cached,
		}}
		responses := AnthropicToResponsesResponse(anthropic)
		stream := NewAnthropicEventToResponsesState()
		stream.InputTokens, stream.OutputTokens, stream.CacheReadInputTokens = 100-cached, 5, cached
		completed := makeResponsesCompletedEvent(stream, "completed", nil)
		for _, response := range []*ResponsesResponse{responses, completed.Response} {
			encoded, err := json.Marshal(response)
			require.NoError(t, err)
			require.True(t, gjson.GetBytes(encoded, "usage.input_tokens_details.cached_tokens").Exists())
			require.Equal(t, int64(cached), gjson.GetBytes(encoded, "usage.input_tokens_details.cached_tokens").Int())
			require.Equal(t, int64(100), gjson.GetBytes(encoded, "usage.input_tokens").Int())
			chat := ResponsesToChatCompletions(response, "example")
			encoded, err = json.Marshal(chat)
			require.NoError(t, err)
			require.True(t, gjson.GetBytes(encoded, "usage.prompt_tokens_details.cached_tokens").Exists())
			require.Equal(t, int64(cached), gjson.GetBytes(encoded, "usage.prompt_tokens_details.cached_tokens").Int())
			require.Equal(t, 105, chat.Usage.TotalTokens, "cache tokens are included in prompt totals, not added twice")
		}
	}
}

func TestCacheUsageKeepsUnreportedBreakdownAbsent(t *testing.T) {
	usage := &ChatUsage{PromptTokens: 10, CompletionTokens: 2, TotalTokens: 12,
		CompletionTokensDetails: &ChatTokenDetails{ReasoningTokens: 1}}
	encoded, err := json.Marshal(usage)
	require.NoError(t, err)
	require.False(t, gjson.GetBytes(encoded, "prompt_tokens_details").Exists())
	require.False(t, gjson.GetBytes(encoded, "completion_tokens_details.cached_tokens").Exists())
	require.Nil(t, ChatUsageToResponsesUsage(usage).InputTokensDetails)

	usage.PromptTokensDetails = &ChatTokenDetails{AudioTokens: 4}
	responses := ChatUsageToResponsesUsage(usage)
	require.NotNil(t, responses.InputTokensDetails)
	require.Equal(t, 4, responses.InputTokensDetails.AudioTokens)
	encoded, err = json.Marshal(usage)
	require.NoError(t, err)
	require.True(t, gjson.GetBytes(encoded, "prompt_tokens_details.cached_tokens").Exists())
	require.Zero(t, gjson.GetBytes(encoded, "prompt_tokens_details.cached_tokens").Int())
	require.False(t, gjson.GetBytes(encoded, "completion_tokens_details.cached_tokens").Exists())
}
