package service

import (
	"encoding/json"
	"fmt"
	"strings"

	"github.com/Wei-Shaw/sub2api/internal/pkg/antigravity"
	"github.com/tidwall/gjson"
)

// A zero budget is significant to these routes. The typed transformer omits
// zero-valued budgets, which otherwise turns an off switch back into a default.
func applyAntigravityClaudeThinkingOff(outbound, original []byte, model string) ([]byte, error) {
	if !strings.HasPrefix(model, "claude-") || readGeminiRequestReasoning(original).Effort != "none" {
		return outbound, nil
	}
	var envelope map[string]any
	if err := json.Unmarshal(outbound, &envelope); err != nil {
		return nil, err
	}
	request, _ := envelope["request"].(map[string]any)
	if request == nil {
		return nil, fmt.Errorf("missing Claude upstream request envelope")
	}
	config, _ := request["generationConfig"].(map[string]any)
	if config == nil {
		config = map[string]any{}
		request["generationConfig"] = config
	}
	config["thinkingConfig"] = map[string]any{"thinkingBudget": 0, "includeThoughts": false}
	return json.Marshal(envelope)
}

// The Google-hosted Claude routes expose a token budget instead of Anthropic's
// adaptive effort field. Read the original controls before conversion can lose
// output_config.effort, and keep explicitly supplied budgets authoritative.
func applyAntigravityClaudeReasoning(req *antigravity.ClaudeRequest, original []byte, mappedModel string) error {
	if !strings.HasPrefix(mappedModel, "claude-") {
		return nil
	}
	control := readGeminiRequestReasoning(original)
	if control.Effort == "none" {
		req.Thinking = &antigravity.ThinkingConfig{Type: "disabled"}
		return nil
	}
	if req.Thinking == nil && !gjson.GetBytes(original, "reasoning_effort").Exists() &&
		!gjson.GetBytes(original, "reasoning.effort").Exists() {
		// Claude Code can leave output_config.effort present while thinking is off.
		return nil
	}
	if control.Budget != nil {
		req.Thinking = &antigravity.ThinkingConfig{Type: "enabled", BudgetTokens: *control.Budget}
		return nil
	}
	budget := 0
	switch control.Effort {
	case "":
		return nil
	case "auto":
		budget = -1
	case "minimal", "low":
		budget = 1024
	case "medium":
		budget = 4096
	case "high":
		budget = 10240
	case "xhigh", "max":
		budget = 32768
	default:
		return fmt.Errorf("unsupported Claude reasoning effort %q", control.Effort)
	}
	req.Thinking = &antigravity.ThinkingConfig{Type: "enabled", BudgetTokens: budget}
	return nil
}

func antigravityClaudeReasoningMetadata(original []byte) *string {
	control := readGeminiRequestReasoning(original)
	if control.Budget != nil {
		effort := "auto"
		switch budget := *control.Budget; {
		case budget == 0:
			effort = "none"
		case budget > 0 && budget <= 1024:
			effort = "low"
		case budget > 0 && budget <= 4096:
			effort = "medium"
		case budget > 0 && budget <= 10240:
			effort = "high"
		case budget > 10240:
			effort = "max"
		}
		return &effort
	}
	if control.Effort == "" {
		return nil
	}
	if control.Effort == "xhigh" {
		control.Effort = "max"
	}
	return &control.Effort
}
