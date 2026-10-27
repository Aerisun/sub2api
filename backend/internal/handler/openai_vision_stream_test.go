//go:build unit

package handler

import (
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/service"
	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
)

func TestAuxiliaryVisionMessagesHeartbeatGetsTerminalErrorAndStillAllowsFailover(t *testing.T) {
	gin.SetMode(gin.TestMode)
	rec := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(rec)
	c.Request = httptest.NewRequest(http.MethodPost, "/v1/messages", nil)
	service.MarkOpenAICompactClientStream(c)
	before := service.OpenAICompactKeepaliveAdjustedWrittenSize(c)
	stop := service.StartOpenAICompactSSEKeepalive(c, 5*time.Millisecond)
	time.Sleep(20 * time.Millisecond)
	stop()
	require.True(t, c.Writer.Written())
	require.Equal(t, before, service.OpenAICompactKeepaliveAdjustedWrittenSize(c))
	require.True(t, openAIForwardMayFailover(c, before, &service.UpstreamFailoverError{StatusCode: 503}))
	h := &OpenAIGatewayHandler{}
	require.True(t, h.ensureAnthropicErrorResponse(c, false))
	require.Contains(t, rec.Body.String(), "event: error\ndata:")
	require.Contains(t, rec.Body.String(), `"type":"error"`)
	require.True(t, service.IsResponseCommitted(c))
	require.False(t, h.ensureAnthropicErrorResponse(c, false))
}
