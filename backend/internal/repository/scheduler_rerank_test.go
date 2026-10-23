//go:build unit

package repository

import (
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/service"
	"github.com/stretchr/testify/require"
)

func TestRerankCapabilitySurvivesSchedulerMetadataProjection(t *testing.T) {
	account := service.Account{ID: 26, Platform: service.PlatformOpenAI, Type: service.AccountTypeAPIKey,
		Credentials: map[string]any{"openai_capabilities": []string{"rerank"},
			"model_mapping": map[string]any{"qwen3-reranker-4b": "qwen3-reranker-4b"},
			"access_token": "unused-secret"}}
	metadata := buildSchedulerMetadataAccount(account)
	require.True(t, metadata.SupportsOpenAIEndpointCapability(service.OpenAIEndpointCapabilityRerank))
	require.False(t, metadata.SupportsOpenAIEndpointCapability(service.OpenAIEndpointCapabilityEmbeddings))
	require.False(t, metadata.SupportsOpenAIEndpointCapability(service.OpenAIEndpointCapabilityChatCompletions))
	require.NotContains(t, metadata.Credentials, "access_token")
}
