package admin

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/pkg/antigravity"
	"github.com/Wei-Shaw/sub2api/internal/service"
	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
)

type antigravityModelsAdminStub struct {
	service.AdminService
	account *service.Account
}

func (s antigravityModelsAdminStub) GetAccount(context.Context, int64) (*service.Account, error) {
	return s.account, nil
}

func TestAntigravityAccountModelsUsesConfiguredMapping(t *testing.T) {
	gin.SetMode(gin.TestMode)
	for _, mapping := range []any{
		map[string]any{"claude-sonnet-4-6": "claude-sonnet-4-6", "gemini-3.5-flash-lite": "gemini-3.5-flash-lite", "gemini-3.8-flash": "gemini-3.8-flash-low"},
		map[string]string{"claude-sonnet-4-6": "claude-sonnet-4-6", "gemini-3.5-flash-lite": "gemini-3.5-flash-lite", "gemini-3.8-flash": "gemini-3.8-flash-low"},
	} {
		h := &AccountHandler{adminService: antigravityModelsAdminStub{account: &service.Account{
			ID: 1, Platform: service.PlatformAntigravity, Type: service.AccountTypeOAuth,
			Credentials: map[string]any{"model_mapping": mapping},
		}}}
		response := httptest.NewRecorder()
		c, _ := gin.CreateTestContext(response)
		c.Params = gin.Params{{Key: "id", Value: "1"}}
		c.Request = httptest.NewRequest(http.MethodGet, "/api/v1/admin/accounts/1/models", nil)
		h.GetAvailableModels(c)
		require.Equal(t, http.StatusOK, response.Code)
		var body struct {
			Data []antigravity.ClaudeModel `json:"data"`
		}
		require.NoError(t, json.Unmarshal(response.Body.Bytes(), &body))
		ids := make([]string, 0, len(body.Data))
		for _, model := range body.Data {
			ids = append(ids, model.ID)
		}
		require.Equal(t, []string{"claude-sonnet-4-6", "gemini-3.5-flash-lite", "gemini-3.8-flash"}, ids)
		require.NotContains(t, response.Body.String(), "gemini-3.7-flash")
		require.NotContains(t, response.Body.String(), "gemini-3.8-flash-low")
		require.NotContains(t, response.Body.String(), "claude-fable")
	}
}

func TestAntigravityAccountModelsKeepsUnconfiguredFallback(t *testing.T) {
	for _, credentials := range []map[string]any{nil, {}, {"model_mapping": map[string]any{}}} {
		require.Equal(t, antigravity.DefaultModels(), antigravityModelsForAccount(&service.Account{Credentials: credentials}))
	}
}
