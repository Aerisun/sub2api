//go:build unit

package middleware

import (
	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/Wei-Shaw/sub2api/internal/service"
	"github.com/gin-gonic/gin"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestChatCredentialRejectedAtPublicGateway(t *testing.T) {
	gin.SetMode(gin.TestMode)
	r := gin.New()
	// An empty service is intentional: rejected internal keys must never reach
	// persistence/cache lookup even if an attacker somehow learns the secret.
	r.Use(apiKeyAuthWithSubscription(&service.APIKeyService{}, nil, &config.Config{}))
	r.POST("/v1/chat/completions", func(c *gin.Context) { c.Status(200) })
	for _, header := range []string{"Authorization", "x-api-key", "x-goog-api-key"} {
		req := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", nil)
		value := service.ChatKeyPrefix + strings.Repeat("c", 46)
		if header == "Authorization" {
			value = "Bearer " + value
		}
		req.Header.Set(header, value)
		out := httptest.NewRecorder()
		r.ServeHTTP(out, req)
		if out.Code != http.StatusUnauthorized {
			t.Fatalf("%s: got %d", header, out.Code)
		}
	}
}
