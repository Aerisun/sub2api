//go:build unit

package service

import (
	"context"
	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
	"io"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func TestVisionToolStreamsRealDeltasBeforeCompletion(t *testing.T) {
	reader, writer := io.Pipe()
	parts := make(chan string, 2)
	finished := make(chan error, 1)
	go func() {
		raw, err := readGLMVisionStream(reader, func(part string) { parts <- part })
		if err == nil && gjson.GetBytes(raw, "choices.0.message.content").String() != "红色三角形" {
			t.Error("description was corrupted")
		}
		finished <- err
	}()
	go func() {
		_, _ = io.WriteString(writer, "data: {\"choices\":[{\"delta\":{\"content\":\"红色\"}}]}\n\n")
	}()
	select {
	case part := <-parts:
		require.Equal(t, "红色", part)
	case <-time.After(time.Second):
		t.Fatal("first real delta was buffered until completion")
	}
	select {
	case <-finished:
		t.Fatal("partial upstream must remain open")
	default:
	}
	_, _ = io.WriteString(writer, "data: {\"choices\":[{\"delta\":{\"content\":\"三角形\"},\"finish_reason\":\"stop\"}]}\n\ndata: {\"choices\":[],\"usage\":{\"prompt_tokens\":20,\"completion_tokens\":3}}\n\ndata: [DONE]\n\n")
	writer.Close()
	require.NoError(t, <-finished)
	require.Equal(t, "三角形", <-parts)
	_, err := readGLMVisionStream(strings.NewReader("data: {\"choices\":[{\"delta\":{\"content\":\"partial\"}}]}\n\n"), nil)
	require.Error(t, err)
}
func TestVisionToolLifecycleAndCacheReuse(t *testing.T) {
	account, _, svc, upstream := glmVisionFixture()
	upstream.resp.Header.Set("Content-Type", "text/event-stream")
	upstream.resp.Body = io.NopCloser(strings.NewReader("data: {\"choices\":[{\"delta\":{\"content\":\"red \"}}]}\n\ndata: {\"choices\":[{\"delta\":{\"content\":\"triangle\"},\"finish_reason\":\"stop\"}]}\n\ndata: {\"choices\":[],\"usage\":{\"prompt_tokens\":20,\"completion_tokens\":3}}\n\ndata: [DONE]\n\n"))
	body := []byte(`{"model":"glm-5.3","stream":true,"messages":[{"role":"user","content":[{"type":"image_url","image_url":{"url":"data:image/png;base64,AAAA"}}]}]}`)
	for attempt := 0; attempt < 2; attempt++ {
		w := httptest.NewRecorder()
		c, _ := gin.CreateTestContext(w)
		c.Set("api_key", &APIKey{ID: 88, UserID: 4})
		stop := startGLMVisionKeepalive(c, account, body, true)
		_, err := svc.bridgeGLMImageInputs(context.Background(), c, account, body)
		stop()
		require.NoError(t, err)
		output := w.Body.String()
		require.Contains(t, output, "event: aerisun.tool")
		require.Contains(t, output, `"status":"completed"`)
		if attempt == 0 {
			require.Contains(t, output, `"delta":"red "`)
			require.True(t, gjson.GetBytes(upstream.lastBody, "stream").Bool())
		} else {
			require.Contains(t, output, `"cached":true`)
			require.NotContains(t, output, `"delta"`)
		}
	}
}
