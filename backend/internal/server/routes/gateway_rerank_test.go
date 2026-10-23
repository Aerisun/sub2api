//go:build unit

package routes

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/service"
	"github.com/stretchr/testify/require"
)

func TestGatewayRerankRoutesAndModelPermissions(t *testing.T) {
	router := newGatewayRoutesTestRouterWithGroup(allowlistGroup(service.PlatformOpenAI, true, "qwen3-embedding-4b"))
	registered := map[string]bool{}
	for _, route := range router.Routes() {
		if route.Method == http.MethodPost {
			registered[route.Path] = true
		}
	}
	for _, path := range []string{"/v1/rerank", "/rerank", "/v1/reranking", "/reranking"} {
		require.True(t, registered[path], path)
		request := httptest.NewRequest(http.MethodPost, path,
			strings.NewReader(`{"model":"qwen3-reranker-4b","query":"中文","documents":["文档"]}`))
		request.Header.Set("Content-Type", "application/json")
		response := httptest.NewRecorder()
		router.ServeHTTP(response, request)
		require.Equal(t, http.StatusNotFound, response.Code, path)
		require.Equal(t, service.CompositeRouteEndpointRerank, compositeRouteEndpointForPath(path))
	}
}

func TestGatewayRerankRejectsUnsupportedPlatform(t *testing.T) {
	router := newGatewayRoutesTestRouter(service.PlatformAnthropic)
	request := httptest.NewRequest(http.MethodPost, "/v1/rerank",
		strings.NewReader(`{"model":"qwen3-reranker-4b","query":"中文","documents":["文档"]}`))
	response := httptest.NewRecorder()
	router.ServeHTTP(response, request)
	require.Equal(t, http.StatusNotFound, response.Code)
}
