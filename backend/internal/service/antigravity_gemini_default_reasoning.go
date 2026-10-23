package service

import (
	"encoding/json"
	"strings"
)

// The account's default selects both the private route and the generation
// controls. Selecting a -low route alone still lets Google use dynamic thinking.
// Explicit client levels, budgets and the thinking-off switch retain precedence.
func antigravityGeminiDefaultReasoning(account *Account, model string, original []byte) string {
	model = strings.TrimPrefix(model, "models/")
	if account == nil || !strings.HasPrefix(model, "gemini-") || geminiRequestedThinkingLevel(original) != "" {
		return ""
	}
	models, _ := account.Credentials["gemini_reasoning_models"].(map[string]any)
	options, _ := models[model].(map[string]any)
	effort, _ := options["default"].(string)
	if _, configured := configuredGeminiReasoningModel(account, model, effort); !configured {
		return ""
	}
	return effort
}

func applyAntigravityGeminiRequestReasoning(outbound, original []byte, model string, account *Account) ([]byte, error) {
	if effort := antigravityGeminiDefaultReasoning(account, model, original); effort != "" {
		var request map[string]any
		if err := json.Unmarshal(original, &request); err != nil {
			return nil, err
		}
		if request == nil {
			return applyGeminiRequestReasoning(outbound, original, model)
		}
		request["reasoning_effort"] = effort
		normalized, err := json.Marshal(request)
		if err != nil {
			return nil, err
		}
		return applyGeminiRequestReasoning(outbound, normalized, model)
	}
	return applyGeminiRequestReasoning(outbound, original, model)
}

func antigravityGeminiReasoningMetadata(original []byte, account *Account, model string) *string {
	if effort := geminiRequestReasoningMetadata(original); effort != nil {
		return effort
	}
	if effort := antigravityGeminiDefaultReasoning(account, model, original); effort != "" {
		effort = geminiEffectiveReasoningEffort(effort)
		return &effort
	}
	return nil
}
