//go:build unit

package service

import (
	"bufio"
	"context"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
)

// The upstream cannot send its answer until the client has read a thought.
// This catches whole-response buffering that accumulated-output tests miss.
func TestReasoningIncrementalAcrossChatProtocols(t *testing.T) {
	gin.SetMode(gin.TestMode)
	for _, model := range []string{"kimi-k3", "kimi-k3-free", "deepseek-v4.1-flash", "glm-5.3", "glm-5.3-flash", "nemotron-3.5-lightning-30b-a3b", "step-5-preview", "qwen3.8-max", "gemini-3.8-flash", "claude-sonnet-4-6"} {
		for _, protocol := range []string{"chat/completions", "messages", "responses"} {
			t.Run(model+"/"+protocol, func(t *testing.T) {
				reader, writer := io.Pipe()
				defer reader.Close()
				gate := make(chan struct{})
				upstream := &httpUpstreamRecorder{resp: &http.Response{StatusCode: 200,
					Header: http.Header{"Content-Type": []string{"text/event-stream"}}, Body: reader}}
				svc := &OpenAIGatewayService{cfg: rawChatCompletionsTestConfig(), httpUpstream: upstream}
				account := forceChatMessagesFallbackAccount()
				go func() {
					defer writer.Close()
					fmt.Fprint(writer, "data: {\"id\":\"cc\",\"choices\":[{\"index\":0,\"delta\":{\"reasoning_content\":\"first thought\"},\"finish_reason\":null}]}\n\n")
					<-gate
					fmt.Fprint(writer, "data: {\"id\":\"cc\",\"choices\":[{\"index\":0,\"delta\":{\"content\":\"OK\"},\"finish_reason\":null}]}\n\ndata: {\"choices\":[{\"index\":0,\"delta\":{},\"finish_reason\":\"stop\"}],\"usage\":{\"prompt_tokens\":3,\"completion_tokens\":3}}\n\ndata: [DONE]\n\n")
				}()
				router := gin.New()
				router.POST("/v1/"+protocol, func(c *gin.Context) {
					body, _ := io.ReadAll(c.Request.Body)
					switch protocol {
					case "messages":
						_, _ = svc.ForwardAsAnthropic(context.Background(), c, account, body, "", "")
					case "responses":
						_, _ = svc.Forward(context.Background(), c, account, body)
					default:
						_, _ = svc.ForwardAsChatCompletions(context.Background(), c, account, body, "", "")
					}
				})
				server := httptest.NewServer(router)
				defer func() {
					close(gate)
					reader.Close()
					server.Close()
				}()
				body := fmt.Sprintf(`{"model":%q,"stream":true,"max_tokens":1024,"max_output_tokens":1024,"reasoning_effort":"low","messages":[{"role":"user","content":"hello"}],"input":"hello"}`, model)
				client := &http.Client{Timeout: 2 * time.Second}
				response, err := client.Post(server.URL+"/v1/"+protocol, "application/json", strings.NewReader(body))
				require.NoError(t, err)
				defer response.Body.Close()
				require.Equal(t, 200, response.StatusCode)
				scanner := bufio.NewScanner(response.Body)
				found := false
				for scanner.Scan() {
					if strings.Contains(scanner.Text(), "first thought") {
						found = true
						break
					}
				}
				require.NoError(t, scanner.Err())
				require.True(t, found, "thought must arrive while answer remains blocked upstream")
			})
		}
	}
}

func TestPoolActualModelIsProvisionedAndTrusted(t *testing.T) {
	account := &Account{Type: "apikey", Credentials: map[string]any{"pool_mode": true,
		"model_mapping": map[string]any{"kimi-k3": "moonshotai/kimi-k3", "kimi-k3-free": "moonshotai/kimi-k3-free"}}}
	headers := make(http.Header)
	headers.Set("X-Sub2api-Upstream-Model", "moonshotai/kimi-k3-free")
	require.Equal(t, "moonshotai/kimi-k3-free", resolvePoolUpstreamModel(account, headers, "moonshotai/kimi-k3"))
	headers.Set("X-Sub2api-Upstream-Model", "unknown")
	require.Equal(t, "moonshotai/kimi-k3", resolvePoolUpstreamModel(account, headers, "moonshotai/kimi-k3"))
	account.Credentials["pool_mode"] = false
	headers.Set("X-Sub2api-Upstream-Model", "moonshotai/kimi-k3-free")
	require.Equal(t, "moonshotai/kimi-k3", resolvePoolUpstreamModel(account, headers, "moonshotai/kimi-k3"))
	detector := newOpenAIChatSilentRefusalDetector(128 * 1024)
	detector.ObserveSSELine(`data: {"choices":[{"delta":{"thinking":"first thought"}}]}`)
	require.True(t, detector.ShouldReleaseClientOutput())
}
