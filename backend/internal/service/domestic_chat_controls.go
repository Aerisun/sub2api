package service

import (
	"encoding/json"
	"fmt"
	"strings"

	"github.com/tidwall/gjson"
	"github.com/tidwall/sjson"
)

// prepareDomesticChatControls is shared by Chat, Messages and Responses. Use
// the original request so generic protocol defaults cannot replace an explicit
// effort, thinking switch, conversation identifier or provider cache TTL.
func prepareDomesticChatControls(chatBody, original []byte, model, protocol string, accounts ...*Account) ([]byte, error) {
	name := strings.ToLower(strings.TrimSpace(model))
	if i := strings.LastIndex(name, "/"); i >= 0 {
		name = name[i+1:]
	}
	if strings.HasPrefix(name, "glm-") || strings.HasPrefix(name, "deepseek-") || strings.HasPrefix(name, "kimi-") || strings.HasPrefix(name, "qwen") || isNvidiaPortableThinkingModel(name) {
		cleaned, err := omitHistoricalChatImages(chatBody)
		if err != nil {
			return nil, err
		}
		chatBody = cleaned
	}
	visionConfigured := len(accounts) > 0 && auxiliaryImageBridgeEnabled(accounts[0], model)
	if (name == "nemotron-3.5-lightning-30b-a3b" || name == "nemotron-3.5") && !visionConfigured {
		for _, message := range gjson.GetBytes(chatBody, "messages").Array() {
			for _, block := range message.Get("content").Array() {
				if kind := block.Get("type").String(); kind == "image_url" || kind == "image" || kind == "input_image" {
					return nil, fmt.Errorf("Nemotron-3.5 does not support image input; select an image-capable model")
				}
			}
		}
	}
	deepseek := strings.HasPrefix(name, "deepseek-v4.1-flash")
	kimi := name == "kimi-k3" || name == "kimi-k3-free"
	portable := isNvidiaPortableThinkingModel(name)
	if !deepseek && !kimi && !portable {
		return chatBody, nil
	}
	var out map[string]json.RawMessage
	if err := json.Unmarshal(chatBody, &out); err != nil {
		return nil, err
	}
	for _, field := range []string{"thinking", "enable_thinking", "prompt_cache_options", "prompt_cache_key", "metadata", "conversation_id"} {
		if value := gjson.GetBytes(original, field); value.Exists() && value.Type != gjson.Null {
			out[field] = json.RawMessage(value.Raw)
		}
	}
	// Kimi's Messages cache_control and OpenAI prompt_cache_options describe
	// the same implicit prefix cache. A TTL must never be invented or extended.
	if protocol == "messages" && len(out["prompt_cache_options"]) == 0 {
		if control := gjson.GetBytes(original, "cache_control"); control.IsObject() {
			ttl := control.Get("ttl").String()
			if ttl == "" {
				ttl = "5m"
			}
			if ttl != "5m" && ttl != "1h" {
				return nil, fmt.Errorf("cache_control.ttl must be 5m or 1h")
			}
			out["prompt_cache_options"], _ = json.Marshal(map[string]string{"mode": "implicit", "ttl": ttl})
		}
	}
	paths := []string{"reasoning_effort", "reasoning.effort", "output_config.effort"}
	if protocol == "messages" {
		paths = []string{"output_config.effort", "reasoning_effort", "reasoning.effort"}
	}
	effort := ""
	for _, path := range paths {
		if value := gjson.GetBytes(original, path); value.Type == gjson.String && strings.TrimSpace(value.String()) != "" {
			effort = normalizeEffortToken(value.String())
			break
		}
	}
	disabled := strings.EqualFold(gjson.GetBytes(original, "thinking.type").String(), "disabled") ||
		(gjson.GetBytes(original, "enable_thinking").Exists() && !gjson.GetBytes(original, "enable_thinking").Bool()) || effort == "none" || effort == "off"
	if effort == "" {
		if budget := gjson.GetBytes(original, "thinking.budget_tokens"); budget.Exists() && budget.Int() > 0 {
			switch {
			case budget.Int() <= 1024:
				effort = "low"
			case budget.Int() <= 10240:
				effort = "high"
			default:
				effort = "max"
			}
		}
	}
	if disabled && deepseek {
		out["thinking"] = json.RawMessage(`{"type":"disabled"}`)
		delete(out, "reasoning_effort")
		delete(out, "enable_thinking")
		if raw, ok := out["reasoning"]; ok {
			clean, err := sjson.DeleteBytes(raw, "effort")
			if err != nil {
				return nil, err
			}
			out["reasoning"] = clean
		}
		return json.Marshal(out)
	}
	if portable {
		if effort == "" || effort == "auto" {
			effort = "low"
		}
		if disabled {
			effort = "none"
		}
		out["reasoning_effort"], _ = json.Marshal(effort)
		return json.Marshal(out)
	}
	// K3 always reasons; off selects its documented lightweight
	// mode without substituting another model. Auto also favors low latency.
	if disabled {
		effort = "low"
	}
	switch effort {
	case "", "auto", "adaptive", "minimal", "low":
		effort = "low"
	case "medium", "high":
		effort = "high"
	case "xhigh", "extrahigh":
		if deepseek {
			effort = "high"
		} else {
			effort = "max"
		}
	case "max", "ultra":
		effort = "max"
	default:
		return nil, fmt.Errorf("unsupported reasoning effort %q; use auto, low, medium, high, xhigh or max", effort)
	}
	out["reasoning_effort"], _ = json.Marshal(effort)
	delete(out, "enable_thinking")
	if kimi {
		delete(out, "thinking")
	} else {
		thinking := out["thinking"]
		if !gjson.ParseBytes(thinking).IsObject() {
			thinking = json.RawMessage(`{}`)
		}
		updated, err := sjson.SetBytes(thinking, "type", "enabled")
		if err != nil {
			return nil, err
		}
		out["thinking"] = updated
	}
	if raw, ok := out["reasoning"]; ok {
		clean, err := sjson.DeleteBytes(raw, "effort")
		if err != nil {
			return nil, err
		}
		out["reasoning"] = clean
	}
	return json.Marshal(out)
}

func isNvidiaPortableThinkingModel(model string) bool {
	name := strings.ToLower(lastOpenAIModelSegment(model))
	return strings.HasPrefix(name, "nemotron-3") || strings.HasPrefix(name, "step-5")
}
