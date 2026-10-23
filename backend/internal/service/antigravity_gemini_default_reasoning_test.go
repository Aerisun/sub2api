package service

import (
	"context"
	"encoding/json"
	"net/http"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
)

func geminiDefaultReasoningAccount() *Account {
	a := newAntigravityCompatAccount(AccountTypeOAuth)
	a.Credentials["model_mapping"] = map[string]any{
		"gemini-3.8-flash": "gemini-3.8-flash-low", "gemini-2.5-flash": "gemini-2.5-flash",
	}
	a.Credentials["gemini_reasoning_models"] = map[string]any{
		"gemini-3.8-flash": map[string]any{"default": "low", "low": "gemini-3.8-flash-low", "high": "gemini-3.8-flash-high", "auto": "gemini-3.8-flash-tiered"},
		"gemini-2.5-flash": map[string]any{"default": "none", "none": "gemini-2.5-flash"},
	}
	return a
}

func TestAntigravityGeminiDefaultReasoningAcrossProtocols(t *testing.T) {
	gin.SetMode(gin.TestMode)
	for _, model := range []string{"gemini-3.8-flash", "gemini-2.5-flash"} {
		for _, protocol := range []string{"chat", "responses", "messages", "native"} {
			t.Run(model+"/"+protocol, func(t *testing.T) {
				upstream := &queuedHTTPUpstreamStub{responses: []*http.Response{antigravityCompatSuccessResponse()}}
				svc := newAntigravityCompatService(config.GatewayConfig{MaxLineSize: defaultMaxLineSize}, upstream)
				account := geminiDefaultReasoningAccount()
				body := map[string]any{"model": model, "max_tokens": 8192, "messages": []any{map[string]any{"role": "user", "content": "hello"}}}
				switch protocol {
				case "responses":
					body = map[string]any{"model": model, "max_output_tokens": 8192, "input": "hello"}
				case "native":
					body = map[string]any{"contents": []any{map[string]any{"role": "user", "parts": []any{map[string]any{"text": "hello"}}}}, "generationConfig": map[string]any{"maxOutputTokens": 8192}}
				}
				raw, err := json.Marshal(body)
				require.NoError(t, err)
				c, _ := newAntigravityCompatContext(http.MethodPost, "/test", raw)
				var result *ForwardResult
				switch protocol {
				case "chat":
					result, err = svc.ForwardAsChatCompletions(context.Background(), c, account, raw, nil)
				case "responses":
					result, err = svc.ForwardAsResponses(context.Background(), c, account, raw, nil)
				case "messages":
					result, err = svc.Forward(context.Background(), c, account, raw, false)
				case "native":
					result, err = svc.ForwardGemini(context.Background(), c, account, model, "generateContent", false, raw, false)
				}
				require.NoError(t, err)
				require.NotNil(t, result)
				require.Nil(t, result.RequestedReasoningEffort, "the client did not request an effort")
				require.Len(t, upstream.requestBodies, 1)
				out := upstream.requestBodies[0]
				require.Equal(t, int64(8192), gjson.GetBytes(out, "request.generationConfig.maxOutputTokens").Int())
				thinking := gjson.GetBytes(out, "request.generationConfig.thinkingConfig")
				if model == "gemini-3.8-flash" {
					require.Equal(t, "low", thinking.Get("thinkingLevel").String())
					require.Equal(t, "low", *result.ReasoningEffort)
					require.NotEmpty(t, gjson.GetBytes(out, "request.contents.0.parts.0.text").String())
				} else {
					require.True(t, thinking.Get("includeThoughts").Exists())
					require.False(t, thinking.Get("includeThoughts").Bool())
					require.False(t, thinking.Get("thinkingBudget").Exists())
				}
			})
		}
	}
}

func TestAntigravityGeminiDefaultReasoningPreservesExplicitControls(t *testing.T) {
	a := geminiDefaultReasoningAccount()
	outbound := []byte(`{"model":"gemini-3.8-flash-low","request":{"contents":[],"tools":[{"functionDeclarations":[{"name":"read_file"}]}],"generationConfig":{"maxOutputTokens":8192,"temperature":0.2}}}`)
	for _, body := range []string{
		`{"reasoning_effort":"high"}`, `{"reasoning_effort":"xhigh"}`, `{"reasoning_effort":"max"}`,
		`{"reasoning":{"effort":"auto"}}`,
		`{"thinking":{"type":"enabled","budget_tokens":1234}}`,
		`{"thinking":{"type":"disabled"},"output_config":{"effort":"max"}}`,
		`{"generationConfig":{"thinkingConfig":{"thinkingBudget":-1,"includeThoughts":false}}}`,
	} {
		t.Run(body, func(t *testing.T) {
			expected, err := applyGeminiRequestReasoning(outbound, []byte(body), "gemini-3.8-flash")
			require.NoError(t, err)
			actual, err := applyAntigravityGeminiRequestReasoning(outbound, []byte(body), "gemini-3.8-flash", a)
			require.NoError(t, err)
			require.JSONEq(t, string(expected), string(actual))
		})
	}
	// Unconfigured accounts and other model families keep their upstream defaults.
	for _, account := range []*Account{nil, newAntigravityCompatAccount(AccountTypeOAuth)} {
		actual, err := applyAntigravityGeminiRequestReasoning(outbound, []byte(`{}`), "gemini-3.8-flash", account)
		require.NoError(t, err)
		require.Equal(t, outbound, actual)
	}
	actual, err := applyAntigravityGeminiRequestReasoning(outbound, []byte(`{}`), "claude-sonnet-4-6", a)
	require.NoError(t, err)
	require.Equal(t, outbound, actual)
	// Native controls unrelated to depth are preserved alongside the default level.
	actual, err = applyAntigravityGeminiRequestReasoning(outbound, []byte(`{"generationConfig":{"thinkingConfig":{"includeThoughts":false}}}`), "gemini-3.8-flash", a)
	require.NoError(t, err)
	require.Equal(t, "low", gjson.GetBytes(actual, "request.generationConfig.thinkingConfig.thinkingLevel").String())
	require.False(t, gjson.GetBytes(actual, "request.generationConfig.thinkingConfig.includeThoughts").Bool())
	require.Equal(t, "read_file", gjson.GetBytes(actual, "request.tools.0.functionDeclarations.0.name").String())
	require.Equal(t, 0.2, gjson.GetBytes(actual, "request.generationConfig.temperature").Float())
	actual, err = applyAntigravityGeminiRequestReasoning(outbound, []byte(`null`), "gemini-3.8-flash", a)
	require.NoError(t, err)
	require.Equal(t, outbound, actual, "invalid native envelopes must not panic before validation")
}
