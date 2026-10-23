package routes

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/service"
	"github.com/stretchr/testify/require"
)

func TestGatewayRoutesImageGenerationDisabled(t *testing.T) {
	router := newGatewayRoutesTestRouterWithGroup(&service.Group{Platform: service.PlatformComposite})
	for _, path := range []string{
		"/v1/messages", "/antigravity/v1/messages", "/v1/chat/completions", "/chat/completions",
		"/v1/responses", "/responses", "/backend-api/codex/responses",
		"/v1/images/generations", "/images/generations", "/v1/images/edits", "/images/edits",
		"/v1/images/generations/async", "/images/generations/async",
		"/v1/images/edits/async", "/images/edits/async", "/v1/images/batches",
	} {
		t.Run(path, func(t *testing.T) {
			req := httptest.NewRequest(http.MethodPost, path, strings.NewReader(`{"model":"claude-sonnet-4-6","tools":[{"type":"image_generation"}]}`))
			req.Header.Set("Content-Type", "application/json")
			response := httptest.NewRecorder()
			router.ServeHTTP(response, req)
			require.Equal(t, http.StatusForbidden, response.Code, response.Body.String())
			require.Contains(t, response.Body.String(), service.ImageGenerationPermissionMessage())
		})
	}
}
