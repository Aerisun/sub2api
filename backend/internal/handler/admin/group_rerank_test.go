//go:build unit

package admin

import (
	"bytes"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
)

func TestCompositeRerankEndpointAcceptedByAdminRequests(t *testing.T) {
	gin.SetMode(gin.TestMode)
	for _, target := range []any{&CompositeRouteRequest{}, &CompositeRoutePreviewRequest{}} {
		c, _ := gin.CreateTestContext(httptest.NewRecorder())
		c.Request = httptest.NewRequest(http.MethodPost, "/", bytes.NewBufferString(
			`{"model":"qwen3-reranker-4b","public_model":"qwen3-reranker-4b","target_platform":"openai","endpoint":"rerank"}`))
		c.Request.Header.Set("Content-Type", "application/json")
		require.NoError(t, c.ShouldBindJSON(target))
	}
}
