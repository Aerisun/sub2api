package service

import (
	"testing"

	"github.com/stretchr/testify/require"
)

func TestGPT61TerraPreservesModelAndReasoningControls(t *testing.T) {
	for _, model := range []string{"gpt-6.1-terra", "openai/gpt-6.1-terra", "gpt-6.1-terra-2026-10-26", "gpt-6.1-terra-max"} {
		require.Equal(t, "gpt-6.1-terra", normalizeKnownOpenAICodexModel(model))
		require.Equal(t, "gpt-6.1-terra", normalizeCodexModel(model))
		require.True(t, isOpenAICodexReasoningGPTModel(model))
		require.True(t, isOpenAICodexImageInputModel(model))
		require.True(t, shouldAutoInjectPromptCacheKeyForCompat(model))
	}
	for _, effort := range []string{"none", "low", "medium", "high", "xhigh", "max"} {
		body := []byte(`{"model":"gpt-6.1-terra","reasoning":{"mode":"standard","effort":"` + effort + `"}}`)
		normalized, changed, err := normalizeOpenAIResponsesReasoningMode(body, "gpt-6.1-terra")
		require.NoError(t, err)
		require.False(t, changed)
		require.JSONEq(t, string(body), string(normalized))
	}
	for _, model := range []string{"gpt-6.1-terrain", "gpt-6.1-sol", "gpt-5.6-terra"} {
		require.False(t, isOpenAIGPT61TerraModel(model))
	}
	descriptor := newConfiguredCodexModelDescriptor("gpt-6.1-terra")
	require.Equal(t, "GPT-6.1 Terra", descriptor.DisplayName)
	require.Equal(t, "low", *descriptor.DefaultReasoningLevel)
	require.Equal(t, int64(500_000), descriptor.ContextWindow)
	require.Equal(t, int64(872_000), descriptor.MaxContextWindow)
	require.Equal(t, []string{"none", "low", "medium", "high", "xhigh", "max", "ultra"}, effortsFromConfiguredCodexLevels(descriptor.SupportedReasoningLevels))
	require.Equal(t, "xhigh", *descriptor.MultiAgentReasoningEffort)
	require.True(t, descriptor.SupportsParallelToolCalls)
}

func TestNormalizeKnownOpenAICodexModelGPT6Astra(t *testing.T) {
	for _, model := range []string{"gpt-6-astra", "openai/gpt-6-astra", "OPENAI/GPT-6_ASTRA", "gpt-6", "openai/gpt-6"} {
		require.Equal(t, "gpt-6-astra", normalizeKnownOpenAICodexModel(model))
	}
}

func TestNormalizeKnownOpenAICodexModel_BareGPT56RoutesToSol(t *testing.T) {
	tests := map[string]string{
		"gpt-5.6":            "gpt-5.6-sol",
		"openai/gpt-5.6":     "gpt-5.6-sol",
		"gpt5.6":             "gpt-5.6-sol",
		"gpt-5.6-high":       "gpt-5.6-sol",
		"gpt-5.6-max":        "gpt-5.6-sol",
		"gpt-5.6-2026-07-09": "gpt-5.6-sol",
		"openai/gpt-5.6-max": "gpt-5.6-sol",
	}

	for input, expected := range tests {
		t.Run(input, func(t *testing.T) {
			require.Equal(t, expected, normalizeKnownOpenAICodexModel(input))
		})
	}
}

func TestUsageBillingModelCandidates_BareGPT56IncludesSol(t *testing.T) {
	require.Equal(t,
		[]string{"gpt-5.6", "gpt-5.6-sol"},
		usageBillingModelCandidates("gpt-5.6"),
	)
	require.Equal(t,
		[]string{"openai/gpt-5.6", "gpt-5.6", "gpt-5.6-sol"},
		usageBillingModelCandidates("openai/gpt-5.6"),
	)
}
