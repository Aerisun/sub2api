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

	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
)

func TestCodexChatCacheDoesNotRewriteAssistantAnswerWithSummary(t *testing.T) {
	body := []byte(`{"model":"gpt-6.1-sol","messages":[{"role":"user","content":"question"},{"role":"assistant","content":"original answer","reasoning_content":"separate visible summary"},{"role":"user","content":"followup"}]}`)
	outbound := forwardOAuthChatCompletionsForUpstreamBody(t, body)
	require.Contains(t, string(outbound), "original answer")
	require.NotContains(t, string(outbound), "<thinking>")
	require.NotContains(t, string(outbound), "separate visible summary")
	require.Equal(t, "original answer", gjson.GetBytes(outbound, "input.1.content.0.text").String())
}

func TestCodexChatCacheReusesTurnStateWithinAccountKeyModelAndConfiguration(t *testing.T) {
	gin.SetMode(gin.TestMode)
	account := &Account{ID: 91, Platform: PlatformOpenAI, Type: AccountTypeOAuth,
		Credentials: map[string]any{"access_token": "fixture-token", "chatgpt_account_id": "fixture-account"}}
	upstream := &httpUpstreamRecorder{}
	svc := &OpenAIGatewayService{cfg: rawChatCompletionsTestConfig(), httpUpstream: upstream}
	forward := func(model, effort string, keyID int64, session string) string {
		body := []byte(`{"model":"` + model + `","reasoning_effort":"` + effort + `","messages":[{"role":"user","content":"hello"}],"stream":false}`)
		c, _ := gin.CreateTestContext(httptest.NewRecorder())
		c.Request = httptest.NewRequest(http.MethodPost, "/v1/chat/completions", bytes.NewReader(body))
		c.Set("api_key", &APIKey{ID: keyID})
		headers := make(http.Header)
		headers.Set("Content-Type", "text/event-stream")
		headers.Set("x-codex-turn-state", "fixture-route-state")
		upstream.resp = &http.Response{StatusCode: http.StatusOK, Header: headers,
			Body: io.NopCloser(strings.NewReader(`data: {"type":"response.completed","response":{"id":"resp_fixture","status":"completed","model":"` + model + `","output":[{"type":"message","role":"assistant","content":[{"type":"output_text","text":"OK"}]}],"usage":{"input_tokens":100,"output_tokens":2}}}` + "\n\n"))}
		_, err := svc.ForwardAsChatCompletions(context.Background(), c, account, body, session, "")
		require.NoError(t, err)
		return upstream.lastReq.Header.Get("x-codex-turn-state")
	}
	require.Empty(t, forward("gpt-6.1-sol", "low", 42, "chat-one"))
	require.Equal(t, "fixture-route-state", forward("gpt-6.1-sol", "low", 42, "chat-one"))
	require.Empty(t, forward("gpt-6.1-sol", "low", 43, "chat-one"))
	require.Empty(t, forward("gpt-6.1-sol", "low", 42, "chat-two"))
	require.Empty(t, forward("gpt-6.1-sol", "high", 42, "chat-one"))
	require.Empty(t, forward("gpt-6-luna", "low", 42, "chat-one"))
	account.ID++
	require.Empty(t, forward("gpt-6.1-sol", "low", 42, "chat-one"))
}
