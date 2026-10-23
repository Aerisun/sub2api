package service

import (
	"fmt"
	"strings"

	"github.com/tidwall/gjson"
	"github.com/tidwall/sjson"
)

// Read the original protocol before compatibility conversion drops tool_choice.
// Keep the established default and the full tool prefix when tools are disabled.
func applyAntigravityClientToolChoice(outbound, original []byte, model string) ([]byte, error) {
	if !strings.HasPrefix(model, "claude-") && !strings.HasPrefix(model, "gemini-") {
		return outbound, nil
	}
	choice := gjson.GetBytes(original, "tool_choice")
	if !choice.Exists() || choice.Type == gjson.Null {
		return outbound, nil
	}
	kind, name := choice.String(), ""
	if choice.IsObject() {
		kind = choice.Get("type").String()
		name = choice.Get("name").String()
		if name == "" {
			name = choice.Get("function.name").String()
		}
	}
	mode := "VALIDATED"
	switch kind {
	case "auto":
	case "none":
		mode = "NONE"
	case "any", "required":
		mode = "ANY"
	case "tool", "function":
		if name == "" {
			return nil, fmt.Errorf("tool_choice requires a function name")
		}
		mode = "ANY"
	default:
		return nil, fmt.Errorf("unsupported tool_choice %q", kind)
	}
	if mode == "ANY" {
		declared, matched := false, false
		for _, tool := range gjson.GetBytes(outbound, "request.tools").Array() {
			for _, function := range tool.Get("functionDeclarations").Array() {
				declared = true
				matched = matched || function.Get("name").String() == name
			}
		}
		if !declared || (name != "" && !matched) {
			return nil, fmt.Errorf("tool_choice must reference a declared function")
		}
	}
	if !gjson.GetBytes(outbound, "request").IsObject() {
		return nil, fmt.Errorf("missing native upstream request")
	}
	calling := map[string]any{"mode": mode}
	if name != "" {
		calling["allowedFunctionNames"] = []string{name}
	}
	// Patch just the choice to avoid decoding and re-encoding image/history payloads.
	return sjson.SetBytes(outbound, "request.toolConfig.functionCallingConfig", calling)
}
