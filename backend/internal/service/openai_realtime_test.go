package service

import (
	"testing"

	"github.com/stretchr/testify/require"
)

func TestRealtimeUsageChargesAudioTextAndCacheExactlyOnce(t *testing.T) {
	frame := []byte(`{"type":"response.done","response":{"id":"resp_example","status":"completed",
		"usage":{"input_tokens":100,"output_tokens":50,
			"input_token_details":{"text_tokens":40,"audio_tokens":50,"image_tokens":10,
				"cached_tokens":25,"cached_tokens_details":{"text_tokens":10,"audio_tokens":10,"image_tokens":5}},
			"output_token_details":{"text_tokens":20,"audio_tokens":30}}}}`)
	result, usage, err := ParseOpenAIRealtimeUsage(frame, "gpt-realtime-2.1-mini")
	require.NoError(t, err)
	require.Equal(t, 100, result.Usage.InputTokens)
	require.Equal(t, 25, result.Usage.CacheReadInputTokens)
	cost, err := usage.cost(result.Model, 2)
	require.NoError(t, err)
	want := (30*.6 + 40*10 + 5*.8 + 10*.06 + 10*.3 + 5*.08 + 20*2.4 + 30*20) / 1e6
	require.InDelta(t, want, cost.TotalCost, 1e-12)
	require.InDelta(t, want*2, cost.ActualCost, 1e-12)
	require.True(t, isForcedUsageBillingRequestID(result.RequestID))
	require.LessOrEqual(t, len(result.RequestID), 64)
	again, _, err := ParseOpenAIRealtimeUsage(frame, result.Model)
	require.NoError(t, err)
	require.Equal(t, result.RequestID, again.RequestID)
}

func TestRealtimeRejectsCompletedResponseWithoutConsistentUsage(t *testing.T) {
	for _, frame := range []string{
		`{"type":"response.done","response":{"id":"r","status":"completed"}}`,
		`{"type":"response.done","response":{"id":"r","status":"completed","usage":{"input_tokens":19,"output_tokens":0,"input_token_details":{"text_tokens":2}}}}`,
		`{"type":"response.done","response":{"id":"r","status":"completed","usage":{"input_tokens":5,"output_tokens":0,"input_token_details":{"text_tokens":5,"cached_tokens":6,"cached_tokens_details":{"text_tokens":6}}}}}`,
	} {
		_, _, err := ParseOpenAIRealtimeUsage([]byte(frame), "gpt-realtime-2.1")
		require.Error(t, err)
	}
}
