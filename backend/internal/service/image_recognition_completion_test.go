//go:build unit

package service

import (
	"bytes"
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
)

func TestRecognitionCompletionRequiresFinishAndUsageEvenAfterDisconnect(t *testing.T) {
	gin.SetMode(gin.TestMode)
	content := `data: {"choices":[{"index":0,"delta":{"content":"red"}}]}` + "\n\n"
	usage := `data: {"choices":[],"usage":{"prompt_tokens":10,"completion_tokens":2,"total_tokens":12}}` + "\n\n"
	finish := `data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}` + "\n\n"
	failed := `data: {"error":{"message":"provider failed"}}` + "\n\n"
	for _, test := range []struct {
		name, stream              string
		cancel, disconnect, valid bool
	}{
		{name: "complete", stream: content + finish + usage + "data: [DONE]\n\n", valid: true},
		{name: "usage and done are not completed image", stream: content + usage + "data: [DONE]\n\n"},
		{name: "missing usage", stream: content + finish + "data: [DONE]\n\n"},
		{name: "explicit failure after terminal data", stream: content + finish + usage + failed + "data: [DONE]\n\n"},
		{name: "client canceled during image", stream: content, cancel: true},
		{name: "client left during image", stream: content, disconnect: true},
	} {
		t.Run(test.name, func(t *testing.T) {
			body := []byte(`{"model":"deepseek-v4-pro","messages":[{"role":"user","content":"fixture"}],"stream":true}`)
			c, _ := gin.CreateTestContext(httptest.NewRecorder())
			c.Request = httptest.NewRequest(http.MethodPost, "/v1/images/recognitions", bytes.NewReader(body))
			c.Set("standard_tool.image_recognition_count", 1)
			if test.disconnect {
				c.Writer = &openAIRawStreamDisconnectedWriter{ResponseWriter: c.Writer}
			}
			reader := io.NopCloser(strings.NewReader(test.stream))
			if test.cancel {
				reader = &openAIChatStreamReadErrorCloser{payload: []byte(test.stream), err: context.Canceled}
			}
			svc := &OpenAIGatewayService{cfg: rawChatCompletionsTestConfig(), httpUpstream: &httpUpstreamRecorder{resp: &http.Response{
				StatusCode: http.StatusOK, Header: http.Header{"Content-Type": []string{"text/event-stream"}}, Body: reader,
			}}}
			result, err := svc.forwardAsRawChatCompletions(context.Background(), c, rawChatCompletionsTestAccount(), body, "")
			require.NotNil(t, result)
			if test.valid {
				require.NoError(t, err)
				require.Equal(t, 10, result.Usage.InputTokens)
			} else {
				require.Error(t, err, "unfinished images must not enter the successful image billing branch")
			}
		})
	}
}
