package service

import (
	"testing"

	"github.com/stretchr/testify/require"
)

func TestAntigravityStrictMappingDoesNotRestoreRemovedModels(t *testing.T) {
	account := &Account{Platform: PlatformAntigravity, Credentials: map[string]any{
		"model_mapping":                    map[string]any{"claude-sonnet-4-6": "claude-sonnet-4-6", "gemini-3.8-flash": "gemini-3.8-flash-low"},
		"antigravity_strict_model_mapping": true,
		"gemini_reasoning_models": map[string]any{"gemini-3.8-flash": map[string]any{
			"default": "low", "low": "gemini-3.8-flash-low", "high": "gemini-3.8-flash-high",
		}},
	}}
	require.Len(t, account.GetModelMapping(), 2)
	require.False(t, account.IsModelSupported("gemini-3.7-flash"))
	require.False(t, account.IsModelSupported("gemini-3.1-pro-low"))
	model, found := configuredGeminiReasoningModel(account, "gemini-3.8-flash", "max")
	require.True(t, found)
	require.Equal(t, "gemini-3.8-flash-high", model)
	// The cache must observe an in-place flag change as well as map changes.
	account.Credentials["antigravity_strict_model_mapping"] = false
	require.Contains(t, account.GetModelMapping(), "gemini-3.7-flash")
	account.Credentials["antigravity_strict_model_mapping"] = true
	require.Len(t, account.GetModelMapping(), 2)
}

func TestUpstreamModelMismatchRecognizesOnlyObservedGeminiRuntimeAlias(t *testing.T) {
	for _, sent := range []string{"gemini-3.8-flash", "gemini-3.8-flash-low", "gemini-3.8-flash-medium", "gemini-3.8-flash-high", "gemini-3.8-flash-tiered"} {
		mismatch := upstreamModelMismatch(sent, "gemini-3.8-flash-n")
		require.NotNil(t, mismatch)
		require.False(t, *mismatch)
	}
	for _, pair := range [][2]string{
		{"gemini-3.8-flash-low", "gemini-3.7-flash-n"},
		{"gemini-3.8-flash-low", "gemini-3.8-flash-lite-n"},
		{"gemini-3.8-flash-low", "gemini-3.8-flash-high"},
		{"gemini-3.8-flash-low", "gemini-3.8-flash-unknown"},
		{"claude-sonnet-4-6", "gemini-3.8-flash-n"},
	} {
		require.True(t, *upstreamModelMismatch(pair[0], pair[1]))
	}
	observer := &upstreamResponseModelObserver{}
	observer.ObserveGemini([]byte(`{"modelVersion":"gemini-3.8-flash-n"}`))
	require.Equal(t, "gemini-3.8-flash-n", observer.Model(), "raw upstream evidence must be retained")
}
