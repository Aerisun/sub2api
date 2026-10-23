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

	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
)

func TestGLMPreferredChannelReplacesFallbackStickySession(t *testing.T) {
	resetOpenAIAdvancedSchedulerSettingCacheForTest()
	ctx := context.Background()
	groupID := int64(38100)
	for _, loadBatch := range []bool{true, false} {
		for _, model := range []string{"glm-5.2", "glm-5.3", "gpt-5.1"} {
			accounts := newLegacySchedulerDecisionTestAccounts(groupID, true)
			accounts[1].Extra["glm_fallback_disable_sticky"] = true
			svc := newLegacySchedulerDecisionTestService(accounts, loadBatch, schedulerTestConcurrencyCache{})
			require.NoError(t, svc.setStickySessionAccountID(ctx, &groupID, "existing-chat", accounts[1].ID, time.Hour))
			selection, _, err := svc.SelectAccountWithSchedulerForCapability(ctx, &groupID, "", "existing-chat", model, nil,
				OpenAIUpstreamTransportAny, OpenAIEndpointCapabilityChatCompletions, false, false, true)
			require.NoError(t, err)
			require.NotNil(t, selection)
			releaseLegacySchedulerDecisionSelection(selection)
			expected := accounts[0].ID
			if model == "gpt-5.1" {
				expected = accounts[1].ID
			}
			require.Equal(t, expected, selection.Account.ID, model)
		}
	}
}

func TestGLMResponsesThinkingOffSurvivesCompatibilityFilters(t *testing.T) {
	for _, control := range []string{
		`"reasoning":{"effort":"none"}`,
		`"reasoning_effort":"none"`,
		`"reasoning":{"effort":"high"},"thinking":{"type":"disabled","budget_tokens":4096}`,
	} {
		account, _, svc, upstream := glmVisionFixture()
		account.Credentials["model_mapping"] = map[string]any{"glm-5.2": "glm-5.2"}
		body := []byte(`{"model":"glm-5.2","input":"Reply with OK.",` + control + `}`)
		filtered, err := filterOpenAIResponsesNoneReasoningEffortForAccount(account, body)
		require.NoError(t, err)
		require.JSONEq(t, string(body), string(filtered))
		var object map[string]any
		require.NoError(t, decodeOpenAIJSONUseNumber(body, &object))
		deleteOpenAIResponsesNoneReasoningEffortFromObject(account, object)
		if effort, exists := object["reasoning_effort"]; exists {
			require.Equal(t, "none", effort)
		}
		c, _ := gin.CreateTestContext(httptest.NewRecorder())
		c.Request = httptest.NewRequest(http.MethodPost, "/v1/responses", bytes.NewReader(body))
		result, err := svc.forwardResponsesViaRawChatCompletions(context.Background(), c, account, filtered)
		require.NoError(t, err)
		require.Equal(t, "none", gjson.GetBytes(upstream.lastBody, "reasoning_effort").String())
		require.Equal(t, "disabled", gjson.GetBytes(upstream.lastBody, "thinking.type").String())
		require.NotNil(t, result.ReasoningEffort)
		require.Equal(t, "none", *result.ReasoningEffort)
		if strings.Contains(control, "budget_tokens") {
			require.Equal(t, int64(4096), gjson.GetBytes(upstream.lastBody, "thinking.budget_tokens").Int())
		}
	}
}

func TestGLMClientThinkingOffAndEffortPreservation(t *testing.T) {
	for _, model := range []string{"glm-5.2", "glm-5.3"} {
		for _, effort := range []string{"none", "low", "medium", "high", "xhigh", "max"} {
			t.Run(model+"/"+effort, func(t *testing.T) {
				body := `{"reasoning_effort":"` + effort + `","thinking":{"type":"enabled","budget_tokens":2048},"temperature":0.4}`
				got, _ := NormalizeGLMOpenAIReasoningEffort([]byte(body), model)
				require.Equal(t, effort, gjson.GetBytes(got, "reasoning_effort").String())
				require.Equal(t, int64(2048), gjson.GetBytes(got, "thinking.budget_tokens").Int())
				if model == "glm-5.2" && effort == "none" {
					require.Equal(t, "disabled", gjson.GetBytes(got, "thinking.type").String())
				} else {
					require.Equal(t, body, string(got))
				}
			})
		}
	}
	for _, body := range []string{
		`{"thinking":{"type":"disabled"}}`,
		`{"thinking":{"type":"disabled"},"reasoning_effort":"high","reasoning":{"effort":"high"}}`,
		`{"reasoning":{"effort":"none"},"temperature":0.3}`,
	} {
		got, changed := NormalizeGLMOpenAIReasoningEffort([]byte(body), "glm-5.2")
		require.True(t, changed)
		require.Equal(t, "disabled", gjson.GetBytes(got, "thinking.type").String())
		require.Equal(t, "none", gjson.GetBytes(got, "reasoning_effort").String())
		if gjson.Get(body, "reasoning.effort").Exists() {
			require.Equal(t, "none", gjson.GetBytes(got, "reasoning.effort").String())
		}
	}
	body := []byte(`{"thinking":{"type":"disabled"},"reasoning_effort":"none"}`)
	got, changed := NormalizeGLMOpenAIReasoningEffort(body, "glm-5.2")
	require.False(t, changed)
	require.Equal(t, body, got)
}

func TestForwardAsAnthropic_GLMClientThinking(t *testing.T) {
	gin.SetMode(gin.TestMode)
	for _, tc := range []struct {
		model, thinking, effort, wantThinking, wantEffort string
	}{
		{"glm-5.2", "", "high", "disabled", "none"},
		{"glm-5.2", "null", "high", "disabled", "none"},
		{"glm-5.2", "disabled", "high", "disabled", "none"},
		{"glm-5.2", "enabled", "medium", "enabled", "medium"},
		{"glm-5.2", "adaptive", "max", "enabled", "max"},
		{"glm-5.3", "disabled", "low", "enabled", "low"},
		{"glm-5.3", "", "high", "enabled", "high"},
		{"glm-5.3", "adaptive", "max", "enabled", "max"},
	} {
		t.Run(tc.model+"/"+tc.thinking+"/"+tc.effort, func(t *testing.T) {
			body := `{"model":"` + tc.model + `","max_tokens":128,"messages":[{"role":"user","content":"hello"}],"thinking":{"type":"` + tc.thinking + `","budget_tokens":2048},"output_config":{"effort":"` + tc.effort + `"}}`
			if tc.thinking == "" {
				body = `{"model":"` + tc.model + `","max_tokens":128,"messages":[{"role":"user","content":"hello"}],"output_config":{"effort":"` + tc.effort + `"}}`
			} else if tc.thinking == "null" {
				body = strings.Replace(body, `"thinking":{"type":"null","budget_tokens":2048}`, `"thinking":null`, 1)
			}
			rec := httptest.NewRecorder()
			c, _ := gin.CreateTestContext(rec)
			c.Request = httptest.NewRequest(http.MethodPost, "/v1/messages", bytes.NewBufferString(body))
			upstream := &httpUpstreamRecorder{resp: &http.Response{
				StatusCode: http.StatusOK,
				Header:     http.Header{"Content-Type": []string{"application/json"}},
				Body:       io.NopCloser(strings.NewReader(`{"id":"probe","object":"chat.completion","model":"` + tc.model + `","choices":[{"index":0,"message":{"role":"assistant","content":"OK"},"finish_reason":"stop"}],"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}`)),
			}}
			account := forceChatMessagesFallbackAccount()
			account.Credentials["model_mapping"] = map[string]any{tc.model: tc.model}
			svc := &OpenAIGatewayService{cfg: rawChatCompletionsTestConfig(), httpUpstream: upstream}
			result, err := svc.ForwardAsAnthropic(context.Background(), c, account, []byte(body), "", "")
			require.NoError(t, err)
			require.Equal(t, tc.wantThinking, gjson.GetBytes(upstream.lastBody, "thinking.type").String())
			if tc.thinking != "" && tc.thinking != "null" {
				require.Equal(t, int64(2048), gjson.GetBytes(upstream.lastBody, "thinking.budget_tokens").Int())
			}
			require.Equal(t, tc.wantEffort, gjson.GetBytes(upstream.lastBody, "reasoning_effort").String())
			require.NotNil(t, result.ReasoningEffort)
			require.Equal(t, tc.wantEffort, *result.ReasoningEffort)
		})
	}
}
