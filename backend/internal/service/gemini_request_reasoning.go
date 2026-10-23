package service

import (
	"bytes"
	"encoding/json"
	"fmt"
	"strings"
)

// Gemini exposes one public model per family. These controls are read from the
// original client body, before compatibility structs can discard extensions.
type geminiRequestReasoning struct {
	Effort                string
	RequestedEffort       string
	Budget                *int
	Native                map[string]any
	CompatibilityControls bool
}

func readGeminiRequestReasoning(body []byte) geminiRequestReasoning {
	var request map[string]any
	if json.Unmarshal(body, &request) != nil {
		return geminiRequestReasoning{}
	}
	control := geminiRequestReasoning{}
	config, _ := request["generationConfig"].(map[string]any)
	if config == nil {
		config, _ = request["generation_config"].(map[string]any)
	}
	if config != nil {
		control.Native, _ = config["thinkingConfig"].(map[string]any)
		if control.Native == nil {
			control.Native, _ = config["thinking_config"].(map[string]any)
		}
		if control.Native != nil {
			control.Effort, _ = control.Native["thinkingLevel"].(string)
			if control.Effort == "" {
				control.Effort, _ = control.Native["thinking_level"].(string)
			}
			budgetValue := control.Native["thinkingBudget"]
			if budgetValue == nil {
				budgetValue = control.Native["thinking_budget"]
			}
			if budget, ok := asInt(budgetValue); ok {
				control.Budget = &budget
			}
		}
	}
	for _, field := range []string{"output_config", "reasoning"} {
		if nested, ok := request[field].(map[string]any); ok {
			if effort, ok := nested["effort"].(string); ok && effort != "" {
				control.Effort = effort
				control.CompatibilityControls = true
			}
		}
	}
	if effort, ok := request["reasoning_effort"].(string); ok && effort != "" {
		control.Effort = effort
		control.CompatibilityControls = true
	}
	control.Effort = strings.ToLower(strings.TrimSpace(control.Effort))
	// Native protobuf defaults mean "not selected", rather than an effort
	// extension to validate or charge for. Preserve the original native field.
	if !control.CompatibilityControls && control.Effort == "thinking_level_unspecified" {
		control.Effort = ""
	}
	control.RequestedEffort = control.Effort
	if thinking, ok := request["thinking"].(map[string]any); ok {
		control.CompatibilityControls = true
		kind, _ := thinking["type"].(string)
		if budget, ok := asInt(thinking["budget_tokens"]); ok {
			control.Budget = &budget
		}
		if kind == "disabled" {
			control.Effort = "none"
			zero := 0
			control.Budget = &zero
		} else if control.Effort == "" && control.Budget == nil && (kind == "enabled" || kind == "adaptive") {
			control.Effort = "auto"
		}
	}
	if control.Effort == "off" {
		control.Effort = "none"
	}
	if control.Effort == "none" {
		zero := 0
		control.Budget = &zero
	}
	if control.Budget != nil && *control.Budget == 0 {
		control.Effort = "none"
	}
	return control
}

func geminiRequestedThinkingLevel(body []byte) string {
	control := readGeminiRequestReasoning(body)
	// Budgets take precedence in the outbound generation controls; use the same
	// precedence for routing, rather than selecting High for a Low budget.
	if control.Budget != nil {
		switch {
		case *control.Budget < 0:
			return "auto"
		case *control.Budget == 0:
			return "none"
		case *control.Budget <= geminiThinkingBudgetLowMax:
			return "low"
		case *control.Budget <= geminiThinkingBudgetMediumMax:
			return "medium"
		default:
			return "high"
		}
	}
	if control.Effort != "" {
		return geminiEffectiveReasoningEffort(control.Effort)
	}
	return ""
}

// Claude Code's effort scale extends beyond Gemini's native high level. Cap
// those explicit requests at high while preserving the original preference
// separately in usage metadata. Auto remains a distinct dynamic-budget mode.
func geminiEffectiveReasoningEffort(effort string) string {
	switch effort {
	case "xhigh", "max":
		return "high"
	default:
		return effort
	}
}

// Claude Code keeps output_config.effort when its thinking switch is off,
// omitting thinking entirely. Effort alone must not re-enable that switch.
// Explicit gateway/OpenAI effort fields remain supported without thinking.
func geminiClaudeReasoningBody(body []byte) []byte {
	var request map[string]any
	if json.Unmarshal(body, &request) != nil {
		return body
	}
	if request["thinking"] != nil || request["output_config"] == nil ||
		request["reasoning_effort"] != nil || request["reasoning"] != nil {
		return body
	}
	request["thinking"] = map[string]any{"type": "disabled"}
	normalized, err := json.Marshal(request)
	if err != nil {
		return body
	}
	return normalized
}

// An opt-in account table keeps routing variants private and lets an explicit
// effort override a family's default mapping without altering custom aliases.
func configuredGeminiReasoningModel(account *Account, model, effort string) (string, bool) {
	if account == nil {
		return "", false
	}
	models, _ := account.Credentials["gemini_reasoning_models"].(map[string]any)
	options, ok := models[model].(map[string]any)
	if !ok {
		return "", false
	}
	if effort == "" {
		effort, _ = options["default"].(string)
	}
	effort = geminiEffectiveReasoningEffort(effort)
	target, _ := options[effort].(string)
	if target == "" {
		target, _ = options["fallback"].(string)
	}
	return strings.TrimSpace(target), target != ""
}

// Apply controls after protocol conversion, including the Claude v1internal
// envelope. Explicit budgets survive exactly; native config fields survive too.
func applyGeminiRequestReasoning(outbound, original []byte, model string) ([]byte, error) {
	model = strings.TrimPrefix(model, "models/")
	if !strings.HasPrefix(model, "gemini-") {
		return outbound, nil
	}
	control := readGeminiRequestReasoning(original)
	switch geminiEffectiveReasoningEffort(control.Effort) {
	case "", "none", "minimal", "low", "medium", "high", "auto":
	default:
		return nil, fmt.Errorf("unsupported Gemini reasoning_effort %q; use auto, minimal, low, medium, high, xhigh or max (xhigh/max map to high; none only on models supporting disabled thinking)", control.Effort)
	}
	if bytes.Equal(outbound, original) && control.Native != nil && !control.CompatibilityControls &&
		control.Effort != "xhigh" && control.Effort != "max" &&
		control.Effort != "auto" &&
		(control.Budget == nil || control.Effort == "") {
		// Native settings retain their exact spelling and upstream defaults.
		// Only compatibility effort aliases require rewriting these controls.
		return outbound, nil
	}
	control.Effort = geminiEffectiveReasoningEffort(control.Effort)
	if control.Effort == "" && control.Budget == nil && control.Native == nil {
		return outbound, nil
	}
	var envelope map[string]any
	if err := json.Unmarshal(outbound, &envelope); err != nil {
		return nil, err
	}
	request := envelope
	if nested, ok := envelope["request"].(map[string]any); ok {
		request = nested
	}
	config, _ := request["generationConfig"].(map[string]any)
	if config == nil {
		config, _ = request["generation_config"].(map[string]any)
		if config == nil {
			config = map[string]any{}
		}
		request["generationConfig"] = config
	}
	thinking := map[string]any{}
	for key, value := range control.Native {
		thinking[key] = value
	}
	delete(thinking, "thinking_level")
	delete(thinking, "thinking_budget")
	if value, exists := thinking["include_thoughts"]; exists {
		if _, canonical := thinking["includeThoughts"]; !canonical {
			thinking["includeThoughts"] = value
		}
		delete(thinking, "include_thoughts")
	}
	delete(request, "generation_config")
	if _, exists := thinking["includeThoughts"]; !exists {
		thinking["includeThoughts"] = control.Effort != "none"
	}
	if control.Budget != nil {
		thinking["thinkingBudget"] = *control.Budget
		// Level and budget are alternative controls in Google's generation API.
		delete(thinking, "thinkingLevel")
		if *control.Budget == 0 && strings.HasPrefix(model, "gemini-2.5-flash") {
			// Antigravity rejects an explicit zero budget on these routes. The
			// non-thinking route plus includeThoughts=false is its off control.
			delete(thinking, "thinkingBudget")
			thinking["includeThoughts"] = false
		}
	} else if control.Effort != "" {
		if strings.HasPrefix(model, "gemini-2.5-") || control.Effort == "auto" {
			budget := -1
			switch control.Effort {
			case "minimal":
				budget = 512
			case "low":
				budget = 1024
			case "medium":
				budget = 8192
			case "high":
				budget = 24576
			}
			thinking["thinkingBudget"] = budget
			delete(thinking, "thinkingLevel")
		} else {
			thinking["thinkingLevel"] = control.Effort
			delete(thinking, "thinkingBudget")
		}
	}
	config["thinkingConfig"] = thinking
	delete(config, "thinking_config")
	for _, field := range []string{"reasoning_effort", "reasoning", "output_config", "thinking"} {
		delete(request, field)
	}
	return json.Marshal(envelope)
}

func geminiRequestReasoningMetadata(body []byte) *string {
	effort := geminiRequestedThinkingLevel(body)
	if effort == "" {
		return nil
	}
	return &effort
}

func geminiClientReasoningMetadata(body []byte) *string {
	effort := readGeminiRequestReasoning(body).RequestedEffort
	if effort == "" {
		return geminiRequestReasoningMetadata(body)
	}
	if effort == "off" {
		effort = "none"
	}
	return &effort
}

func geminiPublicModelFamily(model string) string {
	if model == "gemini-pro-agent" {
		return "gemini-3.1-pro"
	}
	if model == "gemini-2.5-flash-thinking" {
		return "gemini-2.5-flash"
	}
	if strings.HasPrefix(model, "gemini-") {
		for _, suffix := range geminiThinkingVariantSuffixes {
			if strings.HasSuffix(model, suffix) {
				return strings.TrimSuffix(model, suffix)
			}
		}
	}
	return model
}
