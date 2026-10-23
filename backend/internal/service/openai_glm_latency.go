package service

import (
	"context"
	"log/slog"
	"net/http"
	"net/http/httptrace"
	"sync"
	"time"

	"github.com/tidwall/gjson"
)

// Keep connection/pool waits separate from the provider's response wait. No
// customer content or credentials are logged; this also covers text-only GLM.
func traceGLMUpstreamHeaders(ctx context.Context, account *Account, body []byte) (context.Context, func(*http.Response, error)) {
	model := gjson.GetBytes(body, "model").String()
	if account == nil || !isGLMClientThinkingModel(model) {
		return ctx, func(*http.Response, error) {}
	}
	started := time.Now()
	var mu sync.Mutex
	var connected, written, firstByte time.Time
	var reused bool
	trace := &httptrace.ClientTrace{
		GotConn: func(info httptrace.GotConnInfo) {
			mu.Lock()
			connected, reused = time.Now(), info.Reused
			mu.Unlock()
		},
		WroteRequest: func(httptrace.WroteRequestInfo) {
			mu.Lock()
			written = time.Now()
			mu.Unlock()
		},
		GotFirstResponseByte: func() {
			mu.Lock()
			firstByte = time.Now()
			mu.Unlock()
		},
	}
	finish := func(response *http.Response, err error) {
		mu.Lock()
		defer mu.Unlock()
		elapsed := func(at time.Time) int64 {
			if at.IsZero() {
				return -1
			}
			return at.Sub(started).Milliseconds()
		}
		var responseWait int64 = -1
		if !written.IsZero() && !firstByte.IsZero() {
			responseWait = firstByte.Sub(written).Milliseconds()
		}
		protocol := ""
		status := 0
		if response != nil {
			protocol, status = response.Proto, response.StatusCode
		}
		slog.Info("glm_upstream_header_latency", "account_id", account.ID, "model", model,
			"header_ms", time.Since(started).Milliseconds(), "connection_ready_ms", elapsed(connected),
			"request_written_ms", elapsed(written), "first_byte_ms", elapsed(firstByte),
			"response_wait_ms", responseWait, "connection_reused", reused,
			"protocol", protocol, "status_code", status, "transport_failed", err != nil)
	}
	return httptrace.WithClientTrace(ctx, trace), finish
}
