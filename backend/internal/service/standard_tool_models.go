package service

import "slices"

const (
	ToolWebSearch         = "web_search"
	ToolImageGeneration   = "image_generation"
	ToolImageRecognition  = "image_recognition"
	ToolVisionModel       = "nvidia/nemotron-3.5-super-vl-120b-a12b"
	ToolDefaultImageModel = "gpt-image-2.5-flare"
)

// Legacy vision stays on the recognition endpoint for existing scoped keys.
var ToolVisionModels = []string{ToolVisionModel, "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning", "stepfun-ai/step-5-preview"}

var ToolSearchModels = []string{"gpt-6-luna", "gpt-6.1-sol", "gpt-6.1-terra", "gpt-6-sol", "gpt-6-astra", "gpt-5.6-luna", "gpt-5.6-sol", "gpt-5.6-terra"}
var ToolImageModels = []string{ToolDefaultImageModel, "gpt-image-2.5-sunburst", "gpt-image-2"}

// Public capability IDs are backed by real models; they are not text models.
// Account mappings remain unchanged, so these IDs cannot route a chat request.
func AppendStandardToolModels(models []string) []string {
	result := slices.Clone(models)
	appendOnce := func(name string) {
		if !slices.Contains(result, name) {
			result = append(result, name)
		}
	}
	for _, model := range models {
		if slices.Contains(ToolSearchModels, model) {
			appendOnce(ToolWebSearch)
		}
		if slices.Contains(ToolVisionModels, model) {
			appendOnce(ToolImageRecognition)
		}
		if slices.Contains(ToolImageModels, model) {
			appendOnce(ToolImageGeneration)
		}
	}
	return result
}

// Delegation is restricted to the capability's native endpoint. In particular,
// a search-only/recognition-only key never gains permission to use a text API.
func (k *APIKey) WithToolEndpointPermissions(path string) *APIKey {
	if k == nil {
		return k
	}
	var tool string
	var backing []string
	switch path {
	case "/v1/alpha/search", "/alpha/search", "/backend-api/codex/alpha/search":
		tool, backing = ToolWebSearch, ToolSearchModels
	case "/v1/images/generations", "/images/generations", "/v1/images/edits", "/images/edits", "/v1/images/generations/async", "/images/generations/async":
		tool, backing = ToolImageGeneration, ToolImageModels
	case "/v1/images/recognitions", "/images/recognitions":
		tool, backing = ToolImageRecognition, ToolVisionModels
	default:
		return k
	}
	// Hidden vision backends are delegated only on the image endpoint. The
	// request-local copy keeps them out of the public chat catalogue and cached auth.
	if tool == ToolImageRecognition {
		keyAllowed := len(k.AllowedModels) == 0 || (GroupModelAllowlist{Enabled: true, Models: k.AllowedModels}).Allows(tool)
		groupAllowed := k.Group == nil || k.Group.ModelAllowlist.Allows(tool)
		if !keyAllowed || !groupAllowed {
			return k
		}
		copy := *k
		if len(k.AllowedModels) > 0 {
			copy.AllowedModels = append(slices.Clone(k.AllowedModels), backing...)
		}
		if k.Group != nil {
			group := *k.Group
			group.ModelAllowlist.Models = append(slices.Clone(k.Group.ModelAllowlist.Models), backing...)
			copy.Group = &group
		}
		return &copy
	}
	if len(k.AllowedModels) == 0 {
		return k
	}
	allowed := GroupModelAllowlist{Enabled: true, Models: k.AllowedModels}
	if !allowed.Allows(tool) || (k.Group != nil && !k.Group.ModelAllowlist.Allows(tool)) {
		return k
	}
	copy := *k
	copy.AllowedModels = slices.Clone(k.AllowedModels)
	for _, model := range backing {
		if !slices.Contains(copy.AllowedModels, model) {
			copy.AllowedModels = append(copy.AllowedModels, model)
		}
	}
	return &copy
}
