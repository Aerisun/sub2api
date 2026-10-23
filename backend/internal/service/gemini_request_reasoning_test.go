package service

import (
	"encoding/json"
	"testing"

	"github.com/stretchr/testify/require"
)

func TestGeminiUnifiedReasoningProtocolControls(t *testing.T) {
	for _, input := range []string{
		`{"reasoning_effort":"low"}`,
		`{"reasoning":{"effort":"low"}}`,
		`{"thinking":{"type":"adaptive"},"output_config":{"effort":"low"}}`,
		`{"generationConfig":{"thinkingConfig":{"thinkingLevel":"low","includeThoughts":true}}}`,
	} {
		require.Equal(t, "low", geminiRequestedThinkingLevel([]byte(input)))
		out, err := applyGeminiRequestReasoning([]byte(`{"model":"gemini-3.8-flash-low","request":{"contents":[{"role":"user","parts":[{"text":"hello"}]}],"generationConfig":{"maxOutputTokens":2048}}}`), []byte(input), "gemini-3.8-flash")
		require.NoError(t, err)
		var body map[string]any
		require.NoError(t, json.Unmarshal(out, &body))
		request := geminiReasoningJSONMap(t, body["request"])
		config := geminiReasoningJSONMap(t, request["generationConfig"])
		thinking := geminiReasoningJSONMap(t, config["thinkingConfig"])
		require.Equal(t, float64(2048), config["maxOutputTokens"])
		require.Equal(t, "low", thinking["thinkingLevel"])
		require.Equal(t, true, thinking["includeThoughts"])
		require.Len(t, request["contents"], 1)
	}
}

func TestGeminiUnifiedReasoningPreservesBudgetAndDisable(t *testing.T) {
	for _, tc := range []struct {
		input  string
		budget float64
		level  string
	}{
		{`{"thinking":{"type":"enabled","budget_tokens":4000}}`, 4000, "medium"},
		{`{"thinking":{"type":"disabled"},"output_config":{"effort":"high"}}`, 0, "none"},
		{`{"reasoning_effort":"none"}`, 0, "none"},
		{`{"reasoning_effort":"auto"}`, -1, "auto"},
		{`{"generationConfig":{"thinkingConfig":{"thinkingBudget":1234,"includeThoughts":false}}}`, 1234, "medium"},
	} {
		require.Equal(t, tc.level, geminiRequestedThinkingLevel([]byte(tc.input)))
		out, err := applyGeminiRequestReasoning([]byte(`{"contents":[],"generationConfig":{"temperature":0.25}}`), []byte(tc.input), "gemini-2.5-flash")
		require.NoError(t, err)
		var body map[string]any
		require.NoError(t, json.Unmarshal(out, &body))
		config := geminiReasoningJSONMap(t, body["generationConfig"])
		require.Equal(t, 0.25, config["temperature"])
		thinking := geminiReasoningJSONMap(t, config["thinkingConfig"])
		if tc.level == "none" {
			require.NotContains(t, thinking, "thinkingBudget")
		} else {
			require.Equal(t, tc.budget, thinking["thinkingBudget"])
		}
		require.NotContains(t, thinking, "thinkingLevel")
		if tc.level == "none" {
			require.Equal(t, false, thinking["includeThoughts"])
		}
	}
	_, err := applyGeminiRequestReasoning([]byte(`{}`), []byte(`{"reasoning_effort":"unknown"}`), "gemini-3.8-flash")
	require.ErrorContains(t, err, "unsupported")
	out, err := applyGeminiRequestReasoning([]byte(`{"thinking":{"type":"adaptive"}}`), []byte(`{"reasoning_effort":"xhigh"}`), "claude-sonnet-4-6")
	require.NoError(t, err)
	require.JSONEq(t, `{"thinking":{"type":"adaptive"}}`, string(out))
}

func TestGeminiUnifiedReasoningAccountRouting(t *testing.T) {
	account := newAntigravityAccountWithMapping(map[string]string{"gemini-3.8-flash": "gemini-3.8-flash-low"})
	account.Credentials["gemini_reasoning_models"] = map[string]any{"gemini-3.8-flash": map[string]any{
		"default": "low", "low": "gemini-3.8-flash-low", "medium": "gemini-3.8-flash-medium", "high": "gemini-3.8-flash-high", "auto": "gemini-3.8-flash-tiered",
	}}
	svc := &AntigravityGatewayService{}
	for _, tc := range []struct{ level, suffix string }{{"", "low"}, {"low", "low"}, {"medium", "medium"}, {"high", "high"}, {"xhigh", "high"}, {"max", "high"}, {"auto", "tiered"}} {
		require.Equal(t, "gemini-3.8-flash-"+tc.suffix, svc.getMappedModelForThinkingLevel(account, "gemini-3.8-flash", tc.level))
	}
	require.Equal(t, "gemini-3.1-pro", geminiPublicModelFamily("gemini-pro-agent"))
	require.Equal(t, "gemini-2.5-flash", geminiPublicModelFamily("gemini-2.5-flash-thinking"))
}

func TestGeminiClaudeCodeExtendedEffort(t *testing.T) {
	for _, effort := range []string{"xhigh", "max"} {
		for _, input := range []string{
			`{"reasoning_effort":"` + effort + `"}`,
			`{"reasoning":{"effort":"` + effort + `"}}`,
			`{"thinking":{"type":"adaptive"},"output_config":{"effort":"` + effort + `"}}`,
			`{"generationConfig":{"thinkingConfig":{"thinkingLevel":"` + effort + `"}}}`,
		} {
			t.Run(effort+"/"+input, func(t *testing.T) {
				require.Equal(t, "high", geminiRequestedThinkingLevel([]byte(input)))
				require.Equal(t, effort, *geminiClientReasoningMetadata([]byte(input)))
				require.Equal(t, "high", *geminiRequestReasoningMetadata([]byte(input)))
				for _, model := range []string{"gemini-3.8-flash", "gemini-3.1-pro", "gemini-2.5-flash"} {
					out, err := applyGeminiRequestReasoning([]byte(`{"generationConfig":{"maxOutputTokens":32768}}`), []byte(input), model)
					require.NoError(t, err)
					var body map[string]any
					require.NoError(t, json.Unmarshal(out, &body))
					config := geminiReasoningJSONMap(t, body["generationConfig"])
					require.Equal(t, float64(32768), config["maxOutputTokens"])
					thinking := geminiReasoningJSONMap(t, config["thinkingConfig"])
					if model == "gemini-2.5-flash" {
						require.Equal(t, float64(24576), thinking["thinkingBudget"])
					} else {
						require.Equal(t, "high", thinking["thinkingLevel"])
					}
				}
			})
		}
		for _, tc := range []struct {
			thinking string
			level    string
			budget   float64
		}{
			{`{"type":"enabled","budget_tokens":4000}`, "medium", 4000},
			{`{"type":"disabled"}`, "none", 0},
		} {
			input := []byte(`{"thinking":` + tc.thinking + `,"output_config":{"effort":"` + effort + `"}}`)
			require.Equal(t, tc.level, geminiRequestedThinkingLevel(input))
			require.Equal(t, effort, *geminiClientReasoningMetadata(input))
			out, err := applyGeminiRequestReasoning([]byte(`{}`), input, "gemini-3.8-flash")
			require.NoError(t, err)
			var body map[string]any
			require.NoError(t, json.Unmarshal(out, &body))
			thinking := geminiReasoningJSONMap(t, geminiReasoningJSONMap(t, body["generationConfig"])["thinkingConfig"])
			require.Equal(t, tc.budget, thinking["thinkingBudget"])
			require.NotContains(t, thinking, "thinkingLevel")
		}
	}
}

func TestGeminiNativeThinkingOffKeepsAntigravityWorkaround(t *testing.T) {
	body := []byte(`{"generationConfig":{"thinkingConfig":{"thinkingBudget":0}}}`)
	out, err := applyGeminiRequestReasoning(body, body, "gemini-2.5-flash")
	require.NoError(t, err)
	var request map[string]any
	require.NoError(t, json.Unmarshal(out, &request))
	thinking := geminiReasoningJSONMap(t, geminiReasoningJSONMap(t, request["generationConfig"])["thinkingConfig"])
	require.NotContains(t, thinking, "thinkingBudget")
	require.Equal(t, false, thinking["includeThoughts"])
}

func TestGeminiUnifiedReasoningClaudeCodeOff(t *testing.T) {
	off := []byte(`{"output_config":{"effort":"high"},"messages":[]}`)
	require.Equal(t, "none", geminiRequestedThinkingLevel(geminiClaudeReasoningBody(off)))
	on := []byte(`{"thinking":{"type":"adaptive"},"output_config":{"effort":"low"}}`)
	require.Equal(t, "low", geminiRequestedThinkingLevel(geminiClaudeReasoningBody(on)))
	extension := []byte(`{"reasoning_effort":"low","output_config":{"effort":"high"}}`)
	require.Equal(t, "low", geminiRequestedThinkingLevel(geminiClaudeReasoningBody(extension)))
}

func geminiReasoningJSONMap(t *testing.T, value any) map[string]any {
	t.Helper()
	object, ok := value.(map[string]any)
	require.True(t, ok)
	return object
}
