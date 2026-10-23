package service

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"strings"
	"testing"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
)

func TestAntigravityClaudeEffortReachesUpstreamAcrossProtocols(t *testing.T) {
	gin.SetMode(gin.TestMode)
	for _, model := range []string{"claude-sonnet-4-6", "claude-opus-4-6-thinking"} {
		for _, protocol := range []string{"chat", "responses", "messages"} {
			for _, effort := range []string{"low", "medium", "high", "xhigh", "max", "auto"} {
				t.Run(model+"/"+protocol+"/"+effort, func(t *testing.T) {
					body := map[string]any{"model": model, "max_tokens": 64000,
						"messages": []any{map[string]any{"role": "user", "content": "hello"}}}
					if protocol == "messages" {
						body["thinking"] = map[string]any{"type": "adaptive"}
						body["output_config"] = map[string]any{"effort": effort}
					} else if protocol == "responses" {
						body = map[string]any{"model": model, "input": "hello", "max_output_tokens": 64000, "reasoning": map[string]any{"effort": effort}}
					} else {
						body["reasoning_effort"] = effort
					}
					raw, err := json.Marshal(body)
					require.NoError(t, err)
					upstream := &queuedHTTPUpstreamStub{responses: []*http.Response{antigravityCompatSuccessResponse()}}
					svc := newAntigravityCompatService(config.GatewayConfig{MaxLineSize: defaultMaxLineSize}, upstream)
					c, _ := newAntigravityCompatContext(http.MethodPost, "/test", raw)
					a := newAntigravityCompatAccount(AccountTypeOAuth)
					var result *ForwardResult
					switch protocol {
					case "messages":
						result, err = svc.Forward(context.Background(), c, a, raw, false)
					case "responses":
						result, err = svc.ForwardAsResponses(context.Background(), c, a, raw, nil)
					default:
						result, err = svc.ForwardAsChatCompletions(context.Background(), c, a, raw, nil)
					}
					require.NoError(t, err)
					require.NotNil(t, result)
					require.Len(t, upstream.requestBodies, 1)
					budget := map[string]int64{"low": 1024, "medium": 4096, "high": 10240, "xhigh": 32768, "max": 32768, "auto": -1}[effort]
					require.Equal(t, budget, gjson.GetBytes(upstream.requestBodies[0], "request.generationConfig.thinkingConfig.thinkingBudget").Int())
					require.Equal(t, int64(64000), gjson.GetBytes(upstream.requestBodies[0], "request.generationConfig.maxOutputTokens").Int())
					wantEffort := effort
					if wantEffort == "xhigh" {
						wantEffort = "max"
					}
					require.Equal(t, wantEffort, *result.ReasoningEffort)
				})
			}
		}
	}
}

func TestAntigravityReasoningBudgetsAndOffRemainAuthoritative(t *testing.T) {
	account := geminiDefaultReasoningAccount()
	account.Credentials["gemini_reasoning_models"].(map[string]any)["gemini-3.8-flash"].(map[string]any)["medium"] = "gemini-3.8-flash-medium"
	for _, body := range []string{
		`{"thinking":{"type":"enabled","budget_tokens":1024},"output_config":{"effort":"max"}}`,
		`{"generationConfig":{"thinkingConfig":{"thinkingBudget":1024,"thinkingLevel":"high","includeThoughts":false}}}`,
	} {
		level := geminiRequestedThinkingLevel([]byte(body))
		require.Equal(t, "low", level)
		model, ok := configuredGeminiReasoningModel(account, "gemini-3.8-flash", level)
		require.True(t, ok)
		require.Equal(t, "gemini-3.8-flash-low", model)
		out, err := applyGeminiRequestReasoning([]byte(body), []byte(body), "gemini-3.8-flash")
		require.NoError(t, err)
		require.Equal(t, int64(1024), gjson.GetBytes(out, "generationConfig.thinkingConfig.thinkingBudget").Int())
		require.False(t, gjson.GetBytes(out, "generationConfig.thinkingConfig.thinkingLevel").Exists())
	}
	for _, body := range []string{
		`{"generationConfig":{"thinkingConfig":{"thinkingLevel":"auto"}}}`,
		`{"generation_config":{"thinking_config":{"thinking_level":"auto","include_thoughts":false}}}`,
	} {
		out, err := applyGeminiRequestReasoning([]byte(body), []byte(body), "gemini-3.8-flash")
		require.NoError(t, err)
		require.Equal(t, int64(-1), gjson.GetBytes(out, "generationConfig.thinkingConfig.thinkingBudget").Int())
		require.False(t, gjson.GetBytes(out, "generationConfig.thinkingConfig.thinkingLevel").Exists())
		if strings.Contains(body, "include_thoughts") {
			require.False(t, gjson.GetBytes(out, "generationConfig.thinkingConfig.includeThoughts").Bool())
		}
	}
	_, err := applyGeminiRequestReasoning([]byte(`{"generationConfig":{"thinkingConfig":{"thinkingLevel":"unknown"}}}`), []byte(`{"generationConfig":{"thinkingConfig":{"thinkingLevel":"unknown"}}}`), "gemini-3.8-flash")
	require.Error(t, err, "invalid native effort must fail before an upstream request")
	for _, control := range []string{`"thinking":{"type":"adaptive","budget_tokens":1024},"output_config":{"effort":"max"}`, `"thinking":{"type":"disabled"},"output_config":{"effort":"high"}`, `"output_config":{"effort":"high"}`} {
		body := []byte(`{"model":"claude-opus-4-6-thinking","max_tokens":8192,"messages":[{"role":"user","content":"hello"}],` + control + `}`)
		upstream := &queuedHTTPUpstreamStub{responses: []*http.Response{antigravityCompatSuccessResponse()}}
		svc := newAntigravityCompatService(config.GatewayConfig{MaxLineSize: defaultMaxLineSize}, upstream)
		c, _ := newAntigravityCompatContext(http.MethodPost, "/v1/messages", body)
		result, err := svc.Forward(context.Background(), c, newAntigravityCompatAccount(AccountTypeOAuth), body, false)
		require.NoError(t, err)
		require.NotNil(t, result)
		if strings.Contains(control, "adaptive") {
			require.Equal(t, int64(1024), gjson.GetBytes(upstream.requestBodies[0], "request.generationConfig.thinkingConfig.thinkingBudget").Int())
			require.Equal(t, "low", *result.ReasoningEffort)
		} else {
			thinking := gjson.GetBytes(upstream.requestBodies[0], "request.generationConfig.thinkingConfig")
			require.True(t, thinking.Get("thinkingBudget").Exists())
			require.Equal(t, int64(0), thinking.Get("thinkingBudget").Int())
			require.False(t, thinking.Get("includeThoughts").Bool())
		}
	}
}

func TestAntigravityCapacityRetriesAreBoundedAndDeduplicated(t *testing.T) {
	model := "gemini-capacity-latency-test"
	body := []byte(`{"error":{"status":"UNAVAILABLE","details":[{"@type":"type.googleapis.com/google.rpc.ErrorInfo","reason":"MODEL_CAPACITY_EXHAUSTED","metadata":{"model":"` + model + `"}},{"@type":"type.googleapis.com/google.rpc.RetryInfo","retryDelay":"39s"}]}}`)
	response := func() *http.Response {
		return &http.Response{StatusCode: 503, Header: http.Header{}, Body: io.NopCloser(strings.NewReader(string(body)))}
	}
	modelCapacityExhaustedMu.Lock()
	modelCapacityRetrying[model] = true
	modelCapacityExhaustedMu.Unlock()
	upstream := &queuedHTTPUpstreamStub{responses: []*http.Response{response(), response(), response()}}
	params := antigravityRetryLoopParams{ctx: context.Background(), account: newAntigravityCompatAccount(AccountTypeOAuth), accessToken: "test", action: "streamGenerateContent", httpUpstream: upstream}
	svc := &AntigravityGatewayService{}
	started := time.Now()
	result := svc.handleSmartRetry(params, response(), body, "https://example.invalid", 0, []string{"https://example.invalid"})
	require.Less(t, time.Since(started), 100*time.Millisecond)
	require.Equal(t, 503, result.resp.StatusCode)
	require.Equal(t, 0, upstream.callCount)
	modelCapacityExhaustedMu.Lock()
	delete(modelCapacityRetrying, model)
	modelCapacityExhaustedMu.Unlock()
	t.Cleanup(func() {
		modelCapacityExhaustedMu.Lock()
		delete(modelCapacityRetrying, model)
		delete(modelCapacityExhaustedUntil, model)
		modelCapacityExhaustedMu.Unlock()
	})
	started = time.Now()
	result = svc.handleSmartRetry(params, response(), body, "https://example.invalid", 0, []string{"https://example.invalid"})
	require.Equal(t, 503, result.resp.StatusCode)
	require.Equal(t, 3, upstream.callCount)
	require.Less(t, time.Since(started), 4*time.Second)
	modelCapacityExhaustedMu.RLock()
	require.False(t, modelCapacityRetrying[model])
	require.True(t, time.Now().Before(modelCapacityExhaustedUntil[model]))
	modelCapacityExhaustedMu.RUnlock()
	started = time.Now()
	result = svc.handleSmartRetry(params, response(), body, "https://example.invalid", 0, []string{"https://example.invalid"})
	require.Less(t, time.Since(started), 100*time.Millisecond)
	require.Equal(t, 3, upstream.callCount, "the cooldown must prevent another retry burst")
}

func TestAntigravitySignatureRepairKeepsThinkingOffInRetry(t *testing.T) {
	for _, model := range []string{"claude-opus-4-6-thinking", "gemini-3.8-flash", "gemini-2.5-flash"} {
		t.Run(model, func(t *testing.T) {
			account := geminiDefaultReasoningAccount()
			account.Credentials["model_mapping"].(map[string]any)["claude-opus-4-6-thinking"] = "claude-opus-4-6-thinking"
			account.Credentials["gemini_reasoning_models"].(map[string]any)["gemini-2.5-flash"].(map[string]any)["high"] = "gemini-2.5-flash-thinking"
			upstream := &queuedHTTPUpstreamStub{responses: []*http.Response{
				{StatusCode: 400, Header: http.Header{}, Body: io.NopCloser(strings.NewReader(`{"error":{"message":"Invalid thought signature"}}`))},
				antigravityCompatSuccessResponse(),
			}}
			svc := newAntigravityCompatService(config.GatewayConfig{MaxLineSize: defaultMaxLineSize}, upstream)
			body := []byte(`{"model":"` + model + `","max_tokens":64000,"messages":[{"role":"user","content":"hello"}],"thinking":{"type":"adaptive"},"output_config":{"effort":"high"}}`)
			c, _ := newAntigravityCompatContext(http.MethodPost, "/v1/messages", body)
			result, err := svc.Forward(context.Background(), c, account, body, false)
			require.NoError(t, err)
			require.NotNil(t, result)
			require.Len(t, upstream.requestBodies, 2)
			out := upstream.requestBodies[1]
			thinking := gjson.GetBytes(out, "request.generationConfig.thinkingConfig")
			require.True(t, thinking.Get("includeThoughts").Exists())
			require.False(t, thinking.Get("includeThoughts").Bool())
			if model != "gemini-2.5-flash" {
				require.True(t, thinking.Get("thinkingBudget").Exists())
				require.Equal(t, int64(0), thinking.Get("thinkingBudget").Int())
			}
			require.Equal(t, "none", *result.ReasoningEffort)
			require.Equal(t, "high", *result.RequestedReasoningEffort)
			if model == "gemini-3.8-flash" {
				require.Equal(t, "gemini-3.8-flash-low", result.UpstreamModel)
			}
			if model == "gemini-2.5-flash" {
				require.Equal(t, "gemini-2.5-flash", result.UpstreamModel)
			}
		})
	}
}
