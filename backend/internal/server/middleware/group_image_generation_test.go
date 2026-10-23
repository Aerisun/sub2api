package middleware

import (
	"bytes"
	"compress/gzip"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/service"
	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
)

func imagePolicyRouter(group *service.Group) (*gin.Engine, *string) {
	gin.SetMode(gin.TestMode)
	r := gin.New()
	seen := ""
	r.Use(func(c *gin.Context) {
		c.Set(string(ContextKeyAPIKey), &service.APIKey{Group: group})
	}, GroupImageGeneration())
	handler := func(c *gin.Context) {
		body, err := io.ReadAll(c.Request.Body)
		if err != nil {
			c.Status(http.StatusInternalServerError)
			return
		}
		seen = string(body)
		c.Status(http.StatusOK)
	}
	for _, path := range []string{"/v1/messages", "/antigravity/v1/messages", "/v1/responses", "/responses", "/v1/chat/completions", "/images/generations", "/v1/images/edits", "/v1/images/generations/async", "/v1/images/batches", "/v1/images/batches/1/cancel"} {
		r.POST(path, handler)
	}
	r.POST("/v1beta/models/*modelAction", handler)
	r.POST("/antigravity/v1beta/models/*modelAction", handler)
	return r, &seen
}

func TestGroupImageGenerationDeniesBeforeHandler(t *testing.T) {
	cases := []struct{ name, path, body, errorMarker string }{
		{"native model", "/v1beta/models/gemini-3.1-flash-image:generateContent", `{}`, "PERMISSION_DENIED"},
		{"native stream model", "/antigravity/v1beta/models/gemini-2.5-flash-image-preview:streamGenerateContent", `{}`, "PERMISSION_DENIED"},
		{"native text alias", "/v1beta/models/gemini-3.8-flash:generateContent", `{"generationConfig":{"responseModalities":["TEXT","IMAGE"]}}`, "PERMISSION_DENIED"},
		{"native stream alias", "/antigravity/v1beta/models/gemini-3.8-flash:streamGenerateContent", `{"generation_config":{"response_modalities":["image"]}}`, "PERMISSION_DENIED"},
		{"image config", "/v1beta/models/gemini-3.8-flash:generateContent", `{"generationConfig":{"imageConfig":{"imageSize":"1K"}}}`, "PERMISSION_DENIED"},
		{"duplicate config", "/v1beta/models/gemini-3.8-flash:generateContent", `{"generationConfig":{"responseModalities":["TEXT"]},"generationConfig":{"responseModalities":["IMAGE"]}}`, "PERMISSION_DENIED"},
		{"duplicate modality", "/v1beta/models/gemini-3.8-flash:generateContent", `{"generationConfig":{"responseModalities":["TEXT"],"responseModalities":["IMAGE"]}}`, "PERMISSION_DENIED"},
		{"messages model", "/v1/messages", `{"model":"gemini-3.1-flash-image","stream":true}`, "permission_error"},
		{"claude tool", "/v1/messages", `{"model":"claude-sonnet-4-6","tools":[{"type":"image_generation"}]}`, "permission_error"},
		{"antigravity tool", "/antigravity/v1/messages", `{"model":"claude-sonnet-4-6","tools":[{"type":"image_generation"}]}`, "permission_error"},
		{"responses tool", "/v1/responses", `{"model":"gemini-3.8-flash","tools":[{"type":"image_generation"}]}`, "image_generation_disabled"},
		{"duplicate tools", "/responses", `{"model":"claude-sonnet-4-6","tools":[],"tools":[{"type":"image_generation"}]}`, "image_generation_disabled"},
		{"explicit choice", "/v1/responses", `{"model":"claude-sonnet-4-6","tool_choice":{"type":"namespace","name":"image_gen"}}`, "image_generation_disabled"},
		{"chat model", "/v1/chat/completions", `{"model":"gemini-3.1-flash-image"}`, "image_generation_disabled"},
		{"duplicate model", "/v1/messages", `{"model":"claude-sonnet-4-6","MODEL":"gemini-3.1-flash-image"}`, "permission_error"},
		{"generation endpoint", "/images/generations", `{"model":"claude-sonnet-4-6"}`, "image_generation_disabled"},
		{"editing endpoint", "/v1/images/edits", `{"model":"gemini-3.8-flash"}`, "image_generation_disabled"},
		{"async endpoint", "/v1/images/generations/async", `{}`, "image_generation_disabled"},
		{"batch endpoint", "/v1/images/batches", `{}`, "image_generation_disabled"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			router, seen := imagePolicyRouter(&service.Group{Platform: service.PlatformComposite})
			response := doJSON(t, router, http.MethodPost, tc.path, tc.body)
			require.Equal(t, http.StatusForbidden, response.Code, response.Body.String())
			require.Contains(t, response.Body.String(), tc.errorMarker)
			require.Empty(t, *seen, "denied requests must not reach a handler or upstream")
		})
	}
}

func TestGroupImageGenerationPreservesVisionAndOrdinaryTools(t *testing.T) {
	cases := []struct{ path, body string }{
		{"/v1/messages", `{"model":"claude-sonnet-4-6","messages":[{"role":"user","content":[{"type":"image","source":{"type":"base64","media_type":"image/png","data":"aW1hZ2U="}}]}]}`},
		{"/v1/chat/completions", `{"model":"gemini-3.8-flash","messages":[{"role":"user","content":[{"type":"image_url","image_url":{"url":"data:image/png;base64,aW1hZ2U="}}]}]}`},
		{"/v1beta/models/gemini-3.8-flash:generateContent", `{"contents":[{"parts":[{"inlineData":{"mimeType":"image/png","data":"aW1hZ2U="}}]}],"generationConfig":{"responseModalities":["TEXT"],"thinkingConfig":{"thinkingLevel":"high"}}}`},
		{"/v1/responses", `{"model":"claude-sonnet-4-6","tools":[{"type":"namespace","name":"image_gen","tools":[{"type":"function","name":"text2im"}]}]}`},
		{"/v1/messages", `{"model":"claude-sonnet-4-6","tools":[{"name":"image_generation","input_schema":{"type":"object"}}]}`},
		{"/v1/images/batches/1/cancel", `{}`},
	}
	for _, tc := range cases {
		router, seen := imagePolicyRouter(&service.Group{Platform: service.PlatformComposite})
		response := doJSON(t, router, http.MethodPost, tc.path, tc.body)
		require.Equal(t, http.StatusOK, response.Code, response.Body.String())
		require.Equal(t, tc.body, *seen, "the original body must reach downstream unchanged")
	}
}

func TestGroupImageGenerationEnabledAndUngrouped(t *testing.T) {
	for _, group := range []*service.Group{nil, {AllowImageGeneration: true}} {
		router, seen := imagePolicyRouter(group)
		response := doJSON(t, router, http.MethodPost, "/images/generations", `{"model":"gpt-image-2"}`)
		require.Equal(t, http.StatusOK, response.Code)
		require.NotEmpty(t, *seen)
	}
}

func TestGroupImageGenerationCompressedRequest(t *testing.T) {
	var compressed bytes.Buffer
	writer := gzip.NewWriter(&compressed)
	_, err := io.Copy(writer, strings.NewReader(`{"model":"claude-sonnet-4-6","tools":[{"type":"image_generation"}]}`))
	require.NoError(t, err)
	require.NoError(t, writer.Close())
	router, seen := imagePolicyRouter(&service.Group{})
	req := httptest.NewRequest(http.MethodPost, "/v1/messages", &compressed)
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Content-Encoding", "gzip")
	response := httptest.NewRecorder()
	router.ServeHTTP(response, req)
	require.Equal(t, http.StatusForbidden, response.Code)
	require.Empty(t, *seen)
}
