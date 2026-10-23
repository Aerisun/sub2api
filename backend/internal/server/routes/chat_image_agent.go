package routes

import (
	"github.com/gin-gonic/gin"
	"net/http"
	"net/http/httputil"
	"net/url"
)

// Only login JWTs cross this loopback hop. The agent sends model requests back
// through the authenticated /chat/model/completions route; no API-key bypass.
func chatImageAgentProxy() gin.HandlerFunc {
	target, _ := url.Parse("http://127.0.0.1:8094")
	return chatImageAgentProxyTo(target)
}

func chatImageAgentProxyTo(target *url.URL) gin.HandlerFunc {
	proxy := httputil.NewSingleHostReverseProxy(target)
	standard, ok := http.DefaultTransport.(*http.Transport)
	if !ok {
		standard = &http.Transport{}
	}
	transport := standard.Clone()
	transport.Proxy = nil
	proxy.Transport = transport
	proxy.FlushInterval = -1
	proxy.ErrorHandler = func(w http.ResponseWriter, _ *http.Request, _ error) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusBadGateway)
		_, _ = w.Write([]byte(`{"error":{"message":"对话工具服务暂时不可用，请稍后重试"}}`))
	}
	return func(c *gin.Context) { proxy.ServeHTTP(c.Writer, c.Request) }
}
