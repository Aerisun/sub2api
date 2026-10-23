//go:build unit

package service

import (
	"bytes"
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/pkg/apicompat"
	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
)

func TestCodexSubscriptionMessagesNativePayload(t *testing.T) {
	for _, model := range []string{"gpt-6-sol", "gpt-6.1-sol"} {
		t.Run(model, func(t *testing.T) {
			gin.SetMode(gin.TestMode)
			body := []byte(`{"model":"` + model + `","service_tier":"fast","system":"Stable developer instructions","max_tokens":128,"messages":[{"role":"user","content":"hello"}],"output_config":{"effort":"max"}}`)
			rec := httptest.NewRecorder()
			c, _ := gin.CreateTestContext(rec)
			c.Request = httptest.NewRequest(http.MethodPost, "/v1/messages", bytes.NewReader(body))
			c.Request.Header.Set("Content-Type", "application/json")
			stream := `data: {"type":"response.completed","response":{"id":"resp_native","model":"gpt-6-sol","status":"completed","output":[{"type":"message","role":"assistant","content":[{"type":"output_text","text":"OK"}]}],"usage":{"input_tokens":100,"input_tokens_details":{"cached_tokens":32},"output_tokens":2,"total_tokens":102}}}` + "\n\n"
			upstream := &httpUpstreamRecorder{resp: &http.Response{StatusCode: 200, Header: http.Header{"Content-Type": []string{"text/event-stream"}}, Body: io.NopCloser(strings.NewReader(stream))}}
			svc := &OpenAIGatewayService{httpUpstream: upstream, cfg: rawChatCompletionsTestConfig()}
			account := &Account{ID: 91, Platform: PlatformOpenAI, Type: AccountTypeOAuth, Concurrency: 2, Credentials: map[string]any{"access_token": "fixture-token", "chatgpt_account_id": "fixture-account"}}
			result, err := svc.ForwardAsAnthropic(context.Background(), c, account, body, "", "")
			require.NoError(t, err)
			require.NotNil(t, result)
			require.Equal(t, "max", gjson.GetBytes(upstream.lastBody, "reasoning.effort").String())
			require.Equal(t, "detailed", gjson.GetBytes(upstream.lastBody, "reasoning.summary").String())
			require.Equal(t, "priority", gjson.GetBytes(upstream.lastBody, "service_tier").String())
			require.Equal(t, model, gjson.GetBytes(upstream.lastBody, "model").String())
			require.NotContains(t, string(upstream.lastBody), "prompt_cache_breakpoint", "the subscription route rejects API-only cache controls")
			require.Contains(t, string(upstream.lastBody), "Stable developer instructions")
			require.Equal(t, int64(32), gjson.GetBytes(rec.Body.Bytes(), "usage.cache_read_input_tokens").Int())
		})
	}
}

func TestCodexSubscriptionEffortControls(t *testing.T) {
	for _, model := range []string{"gpt-6.1-sol", "gpt-6-astra", "gpt-6-sol", "gpt-6-luna", "gpt-5.6-terra"} {
		require.True(t, shouldAutoInjectPromptCacheKeyForCompat(model), model)
		for _, effort := range []string{"low", "medium", "high", "xhigh", "max"} {
			req := &apicompat.AnthropicRequest{OutputConfig: &apicompat.AnthropicOutputConfig{Effort: effort}}
			require.Equal(t, effort, openAICompatAnthropicReasoningEffort(req, model, "xhigh"), model+" "+effort)
		}
		req := &apicompat.AnthropicRequest{Thinking: &apicompat.AnthropicThinking{Type: "disabled"}, OutputConfig: &apicompat.AnthropicOutputConfig{Effort: "max"}}
		want := "none"
		if model == "gpt-6-astra" || model == "gpt-6.1-sol" {
			want = "low"
		}
		require.Equal(t, want, openAICompatAnthropicReasoningEffort(req, model, "max"))
	}
	// Omission preserves the model default; a generic Responses client does not
	// have to send Anthropic's thinking object to request an effort.
	require.Equal(t, "medium", openAICompatAnthropicReasoningEffort(&apicompat.AnthropicRequest{}, "gpt-6-sol", "medium"))
}

func TestCodexSubscription429StopsRetriesAcrossCallers(t *testing.T) {
	account := &Account{ID: 91, Platform: PlatformOpenAI, Type: AccountTypeOAuth, Extra: map[string]any{"openai_oauth_429_retry_disabled": true}}
	repo := &oauth429RateLimitRepo{}
	svc := &OpenAIGatewayService{rateLimitService: NewRateLimitService(repo, nil, nil, nil, nil)}
	headers := http.Header{"Retry-After": []string{"90"}}
	require.False(t, svc.ShouldRetryOpenAIOAuth429(account, headers, nil))
	svc.markOpenAIOAuth429RateLimited(context.Background(), account, headers, nil)
	require.True(t, svc.isOpenAIAccountRuntimeBlocked(account))
	require.False(t, svc.shouldRetryOpenAIOAuth429OnSameAccountWithResponse(account, 429, false, headers, nil))
	value, ok := svc.openaiAccountRuntimeBlockUntil.Load(account.ID)
	require.True(t, ok)
	require.Greater(t, time.Until(value.(time.Time)), 89*time.Second)
	_, retryWindowCreated := svc.openaiOAuth429RetryStartedAt.Load(account.ID)
	require.False(t, retryWindowCreated)
}

func TestCodexSubscriptionQuotaWithoutResetIsNotTransient(t *testing.T) {
	for _, code := range []string{"subscription_sharing_usage_limit_exceeded", "usage_limit_reached", "insufficient_quota"} {
		disposition, resetAt := classifyOpenAIOAuth429(nil, []byte(`{"error":{"code":"`+code+`"}}`))
		require.NotEqual(t, openAIOAuth429Transient, disposition)
		require.Nil(t, resetAt, "do not invent an official reset time")
	}
}
