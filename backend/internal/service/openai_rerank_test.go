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

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
)

func TestRerankForwardPreservesScoresMappingAndUsage(t *testing.T) {
	gin.SetMode(gin.TestMode)
	body := []byte(`{"model":"public-rerank","query":"中文查询","documents":["相关文档","无关文档"],"top_n":1}`)
	recorder := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(recorder)
	c.Request = httptest.NewRequest(http.MethodPost, "/v1/rerank", bytes.NewReader(body))
	c.Request.Header.Set("Authorization", "Bearer client-secret")
	response := `{"model":"local-rerank","results":[{"index":0,"relevance_score":0.99}],"usage":{"total_tokens":37}}`
	upstream := &httpUpstreamRecorder{resp: &http.Response{
		StatusCode: http.StatusOK, Header: http.Header{"Content-Type": {"application/json"}},
		Body: io.NopCloser(strings.NewReader(response)),
	}}
	svc := &OpenAIGatewayService{cfg: &config.Config{}, httpUpstream: upstream}
	account := &Account{ID: 42, Platform: PlatformOpenAI, Type: AccountTypeAPIKey,
		Credentials: map[string]any{"api_key": "backend-secret", "base_url": "https://local.example/v1",
			"model_mapping": map[string]any{"public-rerank": "local-rerank"}}}
	result, err := svc.ForwardVectorRequest(context.Background(), c, account, body, "", OpenAIEndpointCapabilityRerank)
	require.NoError(t, err)
	require.Equal(t, http.StatusOK, recorder.Code)
	require.JSONEq(t, response, recorder.Body.String())
	require.Equal(t, "https://local.example/v1/rerank", upstream.lastReq.URL.String())
	require.Equal(t, "Bearer backend-secret", upstream.lastReq.Header.Get("Authorization"))
	require.Equal(t, "local-rerank", gjson.GetBytes(upstream.lastBody, "model").String())
	require.Equal(t, "中文查询", gjson.GetBytes(upstream.lastBody, "query").String())
	require.Equal(t, int64(2), gjson.GetBytes(upstream.lastBody, "documents.#").Int())
	require.Equal(t, int64(1), gjson.GetBytes(upstream.lastBody, "top_n").Int())
	require.Equal(t, "public-rerank", result.Model)
	require.Equal(t, "local-rerank", result.UpstreamModel)
	require.Equal(t, 37, result.Usage.InputTokens)
	require.Zero(t, result.Usage.OutputTokens)
	require.Equal(t, "/v1/rerank", GetActualOpenAIUpstreamEndpoint(c))
}

func TestRerankCapabilityRequiresExplicitAPIKeyOptIn(t *testing.T) {
	account := &Account{Platform: PlatformOpenAI, Type: AccountTypeAPIKey, Credentials: map[string]any{}}
	require.False(t, account.SupportsOpenAIEndpointCapability(OpenAIEndpointCapabilityRerank))
	account.Credentials["openai_capabilities"] = []string{"embeddings"}
	require.False(t, account.SupportsOpenAIEndpointCapability(OpenAIEndpointCapabilityRerank))
	account.Credentials["openai_capabilities"] = []string{"rerank"}
	require.True(t, account.SupportsOpenAIEndpointCapability(OpenAIEndpointCapabilityRerank))
	require.False(t, account.SupportsOpenAIEndpointCapability(OpenAIEndpointCapabilityChatCompletions))
	require.False(t, account.SupportsOpenAIEndpointCapability(OpenAIEndpointCapabilityEmbeddings))
	account.Type = AccountTypeOAuth
	require.False(t, account.SupportsOpenAIEndpointCapability(OpenAIEndpointCapabilityRerank))
	require.Equal(t, CompositeRouteEndpointRerank, normalizeCompositeRouteEndpoint("rerank"))
}
