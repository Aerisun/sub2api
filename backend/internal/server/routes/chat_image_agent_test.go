//go:build unit

package routes

import (
	"context"
	"github.com/gin-gonic/gin"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"
)

func TestChatImageAgentPreservesJWTAndStreaming(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/v1/chat/completions" || r.Header.Get("Authorization") != "Bearer jwt-test" {
			t.Errorf("chat identity or path lost")
		}
		w.Header().Set("Content-Type", "text/event-stream")
		_, _ = w.Write([]byte("data: {\"choices\":[]}\n\ndata: [DONE]\n\n"))
	}))
	defer upstream.Close()
	target, _ := url.Parse(upstream.URL)
	router := gin.New()
	router.POST("/api/v1/chat/completions", chatImageAgentProxyTo(target))
	req := httptest.NewRequest("POST", "/api/v1/chat/completions", strings.NewReader(`{"model":"Nemotron-3.5","stream":true}`))
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	req = req.WithContext(ctx)
	req.Header.Set("Authorization", "Bearer jwt-test")
	out := httptest.NewRecorder()
	router.ServeHTTP(out, req)
	if out.Code != 200 || !strings.Contains(out.Body.String(), "[DONE]") || out.Header().Get("Content-Type") != "text/event-stream" {
		t.Fatalf("SSE response not forwarded: status %d", out.Code)
	}
}
