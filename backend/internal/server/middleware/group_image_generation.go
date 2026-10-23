package middleware

import (
	"net/http"
	"strings"

	"github.com/Wei-Shaw/sub2api/internal/pkg/httputil"
	"github.com/Wei-Shaw/sub2api/internal/service"
	"github.com/gin-gonic/gin"
	"github.com/tidwall/gjson"
)

// GroupImageGeneration enforces the group flag before routing or scheduling,
// including Gemini native output controls and Anthropic-compatible requests.
// Input images and client-side function tools remain ordinary chat requests.
func GroupImageGeneration() gin.HandlerFunc {
	return func(c *gin.Context) {
		key, ok := GetAPIKeyFromContext(c)
		if !ok || key == nil || service.GroupAllowsImageGeneration(key.Group) || c.Request == nil {
			c.Next()
			return
		}
		switch c.Request.Method {
		case http.MethodPost, http.MethodPut, http.MethodPatch:
		default:
			c.Next()
			return
		}
		path := strings.TrimRight(c.Request.URL.Path, "/")
		if isImageCreationPath(path) || isGeminiOrClaudeImageModel(groupModelAllowlistModelFromParams(c)) {
			rejectGroupImageGeneration(c)
			return
		}
		models, done := groupModelAllowlistModelsFromBody(c)
		if !done {
			return
		}
		for _, model := range models {
			if isGeminiOrClaudeImageModel(model) || service.IsExplicitImageGenerationIntent(path, model, nil) {
				rejectGroupImageGeneration(c)
				return
			}
		}
		// The preread body shares the existing buffer with the allowlist and
		// downstream handlers; the policy introduces no body copy or upstream call.
		body, _ := httputil.ReadRequestBodyWithPrealloc(c.Request)
		if service.IsExplicitImageGenerationIntent(path, "", body) || hasNativeImageOutput(body) {
			rejectGroupImageGeneration(c)
			return
		}
		c.Next()
	}
}

func rejectGroupImageGeneration(c *gin.Context) {
	service.MarkOpsClientBusinessLimited(c, service.OpsClientBusinessLimitedReasonLocalFeatureGate)
	path := c.Request.URL.Path
	switch {
	case strings.HasPrefix(path, "/v1beta") || strings.HasPrefix(path, "/antigravity/v1beta"):
		GoogleErrorWriter(c, http.StatusForbidden, service.ImageGenerationPermissionMessage())
	case strings.Contains(path, "/messages"):
		AnthropicErrorWriter(c, http.StatusForbidden, service.ImageGenerationPermissionMessage())
	default:
		c.JSON(http.StatusForbidden, gin.H{"error": gin.H{
			"type": "permission_error", "code": "image_generation_disabled",
			"message": service.ImageGenerationPermissionMessage(),
		}})
	}
	c.Abort()
}

func isImageCreationPath(path string) bool {
	for _, suffix := range []string{"/images/generations", "/images/edits", "/images/generations/async", "/images/edits/async", "/images/batches"} {
		if strings.HasSuffix(path, suffix) {
			return true
		}
	}
	return false
}

func isGeminiOrClaudeImageModel(model string) bool {
	model = strings.TrimPrefix(strings.ToLower(strings.TrimSpace(model)), "models/")
	return strings.HasPrefix(model, "imagen-") ||
		((strings.HasPrefix(model, "gemini-") || strings.HasPrefix(model, "claude-")) && strings.Contains(model, "image"))
}

func imageControlKey(key string) string {
	return strings.ReplaceAll(strings.ToLower(key), "_", "")
}

func hasNativeImageOutput(body []byte) bool {
	if !gjson.ValidBytes(body) {
		return false
	}
	found := false
	// Walk all occurrences so duplicate keys cannot bypass a downstream JSON
	// parser that chooses the last occurrence instead of gjson's first one.
	gjson.ParseBytes(body).ForEach(func(key, value gjson.Result) bool {
		switch imageControlKey(key.Str) {
		case "generationconfig":
			value.ForEach(func(configKey, configValue gjson.Result) bool {
				switch imageControlKey(configKey.Str) {
				case "responsemodalities":
					found = includesImageModality(configValue)
				case "imageconfig":
					found = configValue.IsObject()
				}
				return !found
			})
		case "responsemodalities":
			found = includesImageModality(value)
		case "tools":
			value.ForEach(func(_, tool gjson.Result) bool {
				tool.ForEach(func(toolKey, toolValue gjson.Result) bool {
					found = strings.EqualFold(toolKey.Str, "type") && strings.EqualFold(strings.TrimSpace(toolValue.String()), "image_generation")
					return !found
				})
				return !found
			})
		case "toolchoice":
			found = service.IsExplicitImageGenerationIntent("", "", []byte(`{"tool_choice":`+value.Raw+`}`))
		}
		return !found
	})
	return found
}

func includesImageModality(value gjson.Result) bool {
	if !value.IsArray() {
		return strings.EqualFold(strings.TrimSpace(value.String()), "image")
	}
	found := false
	value.ForEach(func(_, modality gjson.Result) bool {
		found = strings.EqualFold(strings.TrimSpace(modality.String()), "image")
		return !found
	})
	return found
}
