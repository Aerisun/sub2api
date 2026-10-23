package middleware

import (
	"net/http"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/service"
	"github.com/stretchr/testify/require"
)

func TestAPIKeyAllowedModelsGatewayAdmission(t *testing.T) {
	key := allowlistAPIKey(false)
	key.AllowedModels = []string{"claude-sonnet-4-6"}
	router, calls := newGroupModelAllowlistTestRouter(key, "/v1")
	for _, path := range []string{"/v1/messages", "/v1/responses", "/v1/chat/completions", "/v1/embeddings"} {
		require.Equal(t, http.StatusOK, doJSON(t, router, http.MethodPost, path, `{"model":"claude-sonnet-4-6"}`).Code)
		w := doJSON(t, router, http.MethodPost, path, `{"model":"claude-opus-4-6"}`)
		require.Equal(t, http.StatusNotFound, w.Code)
		require.Contains(t, w.Body.String(), "API key")
		require.Equal(t, http.StatusNotFound, doJSON(t, router, http.MethodPost, path, `{"model":"claude-sonnet-4-6","Model":"claude-opus-4-6"}`).Code)
	}
	require.Len(t, *calls, 4)
	require.False(t, key.Group.ModelAllowlistEnabled(), "shared cached group must stay unchanged")
	require.Equal(t, http.StatusNotFound, doJSON(t, router, http.MethodPost, "/v1/models/gemini-3.8-flash:generateContent", `{}`).Code)
	key.Group.ModelAllowlist = service.GroupModelAllowlist{Enabled: true, Models: []string{"gemini-*"}}
	require.Equal(t, http.StatusNotFound, doJSON(t, router, http.MethodPost, "/v1/messages", `{"model":"claude-sonnet-4-6"}`).Code)
	key.AllowedModels = nil
	require.Equal(t, http.StatusOK, doJSON(t, router, http.MethodPost, "/v1/messages", `{"model":"gemini-3.8-flash"}`).Code)
}
