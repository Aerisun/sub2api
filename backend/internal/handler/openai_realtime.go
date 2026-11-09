package handler

import (
	"bufio"
	"context"
	"encoding/json"
	"net/http"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/pkg/ip"
	middleware2 "github.com/Wei-Shaw/sub2api/internal/server/middleware"
	"github.com/Wei-Shaw/sub2api/internal/service"
	coderws "github.com/coder/websocket"
	"github.com/gin-gonic/gin"
	"github.com/tidwall/gjson"
	"go.uber.org/zap"
)

// OpenAIRealtime exposes the standard realtime event protocol. The worker
// translates the verified upstream WebRTC media leg, not its billing events.
func (h *OpenAIGatewayHandler) OpenAIRealtime(c *gin.Context) {
	if !isOpenAIWSUpgradeRequest(c.Request) {
		h.errorResponse(c, http.StatusUpgradeRequired, "invalid_request_error", "WebSocket upgrade required")
		return
	}
	key, ok := middleware2.GetAPIKeyFromContext(c)
	if !ok || key.Group == nil || !key.Group.ModelAllowlistEnabled() ||
		(key.Group.Platform != service.PlatformOpenAI && key.Group.Platform != service.PlatformComposite) {
		h.errorResponse(c, http.StatusForbidden, "permission_error", "Realtime is not enabled for this group")
		return
	}
	model := strings.TrimSpace(c.Query("model"))
	if !service.IsStandardOpenAIRealtimeModel(model) || !key.Group.ModelAllowlist.Allows(model) ||
		!compositeTargetPlatformAllowed(c, key, model, service.PlatformOpenAI) {
		h.errorResponse(c, http.StatusNotFound, "model_not_found", "Realtime model is unavailable")
		return
	}
	subject, ok := middleware2.GetAuthSubjectFromContext(c)
	if !ok || !h.ensureResponsesDependencies(c, nil) {
		return
	}
	ctx := c.Request.Context()
	subscription, _ := middleware2.GetSubscriptionFromContext(c)
	platform := service.QuotaPlatform(ctx, key)
	check := func(ctx context.Context) error {
		return h.billingCacheService.CheckBillingEligibility(ctx, key.User, key, key.Group, subscription, platform)
	}
	if err := check(ctx); err != nil {
		status, code, message, _ := billingErrorDetails(err)
		h.errorResponse(c, status, code, message)
		return
	}
	userRelease, acquired, err := h.concurrencyHelper.TryAcquireUserSlot(ctx, subject.UserID, subject.Concurrency)
	if err != nil || !acquired {
		h.errorResponse(c, http.StatusTooManyRequests, "rate_limit_error", "Realtime concurrency limit reached")
		return
	}
	defer userRelease()

	// Select the requested model, respect existing account limits, and return
	// promptly when busy. Do not replay or migrate accepted audio.
	selection, _, err := h.gatewayService.SelectAccountWithSchedulerForCapability(
		service.WithOpenAIProfitControlSuppressed(ctx), key.GroupID, "", "", model, nil,
		service.OpenAIUpstreamTransportHTTPSSE, service.OpenAIEndpointCapabilityLive,
		false, false, false, service.PlatformOpenAI,
	)
	if selection != nil && selection.ReleaseFunc != nil {
		defer selection.ReleaseFunc()
	}
	if err != nil || selection == nil || selection.Account == nil || !selection.Acquired {
		h.errorResponse(c, http.StatusServiceUnavailable, "api_error", "No available realtime account")
		return
	}
	account := selection.Account
	bridge, err := h.gatewayService.OpenStandardRealtimeBridge(ctx, account, model)
	if err != nil {
		h.errorResponse(c, http.StatusServiceUnavailable, "api_error", "Realtime bridge unavailable")
		return
	}
	defer bridge.Close()
	ws, err := coderws.Accept(c.Writer, c.Request, nil)
	if err != nil {
		return
	}
	defer ws.CloseNow()
	ws.SetReadLimit(1024 * 1024)
	// Keep the billing reader alive briefly after a client drops so a cancelled
	// or completed turn can still be settled from the upstream terminal.
	session, cancel := context.WithTimeout(context.WithoutCancel(ctx), 10*time.Minute+30*time.Second)
	defer cancel()
	_ = bridge.SetReadDeadline(time.Now().Add(10*time.Minute + 30*time.Second))
	var sendMu sync.Mutex
	send := func(msg []byte) error {
		sendMu.Lock()
		defer sendMu.Unlock()
		_ = bridge.SetWriteDeadline(time.Now().Add(2 * time.Second))
		_, err := bridge.Write(append(msg, '\n'))
		return err
	}
	write := func(msg []byte) error {
		wctx, cancel := context.WithTimeout(session, 3*time.Second)
		defer cancel()
		return ws.Write(wctx, coderws.MessageText, msg)
	}
	var responseStarted atomic.Pointer[time.Time]
	readDone := make(chan struct{})
	go func() {
		defer close(readDone)
		defer func() { _ = send([]byte(`{"type":"bridge.disconnect"}`)) }()
		for {
			kind, msg, err := ws.Read(session)
			if err != nil || kind != coderws.MessageText || !json.Valid(msg) {
				return
			}
			switch gjson.GetBytes(msg, "type").String() {
			case "session.update", "conversation.item.create", "input_audio_buffer.append",
				"input_audio_buffer.commit", "input_audio_buffer.clear", "response.cancel":
			case "response.create":
				started := time.Now()
				if !responseStarted.CompareAndSwap(nil, &started) {
					_ = write([]byte(`{"type":"error","error":{"type":"invalid_request_error","message":"Wait for the current response to finish."}}`))
					continue
				}
				if check(session) != nil {
					_ = write([]byte(`{"type":"error","error":{"type":"insufficient_quota","message":"Balance or quota exhausted."}}`))
					return
				}
			default:
				_ = write([]byte(`{"type":"error","error":{"type":"invalid_request_error","message":"Unsupported realtime event."}}`))
				continue
			}
			// Compact JSON so embedded newlines cannot inject IPC frames.
			compact, err := json.Marshal(json.RawMessage(msg))
			if err != nil || send(compact) != nil {
				return
			}
		}
	}()
	reqLog := requestLogger(c, "handler.openai_gateway.realtime")
	userAgent, clientIP := c.Request.UserAgent(), ip.GetClientIP(c)
	scanner := bufio.NewScanner(bridge)
	scanner.Buffer(make([]byte, 16*1024), 1024*1024)
	var firstOutputMs *int
readBridge:
	for scanner.Scan() {
		msg := scanner.Bytes()
		switch gjson.GetBytes(msg, "type").String() {
		case "bridge.create_rejected":
			msg = []byte(gjson.GetBytes(msg, "event").Raw)
			responseStarted.Store(nil)
		case "response.output_audio.delta", "response.output_text.delta":
			if start := responseStarted.Load(); start != nil && firstOutputMs == nil {
				ms := int(time.Since(*start).Milliseconds())
				firstOutputMs = &ms
			}
		case "response.done":
			result, usage, billErr := service.ParseOpenAIRealtimeUsage(msg, model)
			if billErr == nil && result != nil {
				var pricingAt time.Time
				if start := responseStarted.Load(); start != nil {
					pricingAt = *start
					result.Duration = time.Since(*start)
					result.FirstTokenMs = firstOutputMs
				}
				bctx, bcancel := context.WithTimeout(context.WithoutCancel(ctx), 6*time.Second)
				billErr = h.gatewayService.RecordUsage(bctx, &service.OpenAIRecordUsageInput{
					Result: result, RealtimeUsage: usage, APIKey: key, User: key.User, Account: account,
					Subscription: subscription, PricingAt: pricingAt, APIKeyService: h.apiKeyService, QuotaPlatform: platform,
					InboundEndpoint: "/v1/realtime", UpstreamEndpoint: "/backend-api/codex/realtime/calls",
					UserAgent: userAgent, IPAddress: clientIP,
				})
				bcancel()
			}
			if billErr != nil {
				reqLog.Warn("realtime.usage_settlement_failed", zap.Error(billErr))
				_ = write([]byte(`{"type":"error","error":{"type":"server_error","message":"Realtime usage could not be settled."}}`))
				break readBridge
			}
			if send([]byte(`{"type":"bridge.settled"}`)) != nil {
				break readBridge
			}
			responseStarted.Store(nil)
			firstOutputMs = nil
		}
		select {
		case <-readDone:
			// Continue settling any terminal event, without writing to a closed client.
		default:
			if write(msg) != nil {
				_ = ws.CloseNow()
			}
		}
	}
	_ = ws.CloseNow()
	_ = bridge.Close()
	<-readDone
}
