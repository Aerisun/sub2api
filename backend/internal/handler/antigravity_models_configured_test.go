package handler

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	middleware2 "github.com/Wei-Shaw/sub2api/internal/server/middleware"
	"github.com/Wei-Shaw/sub2api/internal/service"
	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
)

func TestAntigravityModelsUsesConfiguredPublicAliases(t *testing.T) {
	gin.SetMode(gin.TestMode)
	groupID := int64(9)
	h := newGatewayModelsHandlerForTest(&gatewayModelsAccountRepoStub{
		byGroup: map[int64][]service.Account{groupID: {
			{ID: 1, Platform: service.PlatformAntigravity,
				Credentials: map[string]any{"model_mapping": map[string]any{
					"claude-sonnet-4-6":     "claude-sonnet-4-6",
					"gemini-3.1-flash-lite": "gemini-3.1-flash-lite",
					"gemini-3.5-flash-lite": "gemini-3.5-flash-lite",
					"gemini-3.8-flash":      "gemini-3.8-flash-low",
					"gemini-3.7-flash":      "gemini-3.7-flash-low",
				}}},
			{ID: 9, Platform: service.PlatformOpenAI,
				Credentials: map[string]any{"model_mapping": map[string]any{"glm-5.3": "glm-5.3"}}},
		}},
	})
	response := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(response)
	c.Request = httptest.NewRequest(http.MethodGet, "/antigravity/models", nil)
	c.Set(string(middleware2.ContextKeyAPIKey), &service.APIKey{GroupID: &groupID,
		Group: &service.Group{ID: groupID, Platform: service.PlatformComposite,
			ModelAllowlist: service.GroupModelAllowlist{Enabled: true, Models: []string{
				"claude-sonnet-4-6", "gemini-3.1-flash-lite", "gemini-3.5-flash-lite", "gemini-3.8-flash", "glm-5.3",
			}}},
	})
	h.AntigravityModels(c)
	require.Equal(t, http.StatusOK, response.Code)
	var body gatewayModelsResponseForTest
	require.NoError(t, json.Unmarshal(response.Body.Bytes(), &body))
	ids := make([]string, 0, len(body.Data))
	for _, model := range body.Data {
		ids = append(ids, model.ID)
	}
	require.ElementsMatch(t, []string{"claude-sonnet-4-6", "gemini-3.1-flash-lite", "gemini-3.5-flash-lite", "gemini-3.8-flash"}, ids)
	require.NotContains(t, response.Body.String(), "gemini-3.8-flash-low")
	require.NotContains(t, response.Body.String(), "gemini-3.7-flash")
	require.NotContains(t, response.Body.String(), "claude-fable")
}
