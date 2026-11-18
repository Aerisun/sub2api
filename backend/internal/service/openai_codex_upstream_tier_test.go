package service

import (
	"bytes"
	"context"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/Wei-Shaw/sub2api/internal/pkg/ctxkey"
	coderws "github.com/coder/websocket"
	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
)

func codexTierGroupContext(standard bool) context.Context {
	return context.WithValue(context.Background(), ctxkey.Group, &Group{
		ID: 103, Platform: PlatformComposite, Status: StatusActive, Hydrated: true,
		OpenAIFastUpstreamStandard: standard,
	})
}

func TestCodexFastUpstreamTier_GroupForwardingKeepsBilling(t *testing.T) {
	gin.SetMode(gin.TestMode)
	for _, mode := range []struct{ model, requested, billed string }{
		{"gpt-6.1-sol", "fast", "priority"},
		{"gpt-6.2-sol", "ultrafast", "ultrafast"},
	} {
		for _, standard := range []bool{false, true} {
			for _, endpoint := range []string{"responses", "passthrough", "chat", "messages"} {
				for _, stream := range []bool{false, true} {
					t.Run(fmt.Sprintf("%s/standard=%t/%s/stream=%t", mode.model, standard, endpoint, stream), func(t *testing.T) {
						ctx := codexTierGroupContext(standard)
						body := []byte(fmt.Sprintf(`{"model":%q,"service_tier":%q,"instructions":"test","input":[{"role":"user","content":"hello"}],"messages":[{"role":"user","content":"hello"}],"max_tokens":100,"stream":%t}`, mode.model, mode.requested, stream))
						response := strings.ReplaceAll(`{"id":"resp_tier","object":"response","model":"gpt-6.1-sol","status":"completed","service_tier":"default","output":[{"type":"message","role":"assistant","content":[{"type":"output_text","text":"hello"}]}],"usage":{"input_tokens":10,"output_tokens":2}}`, "gpt-6.1-sol", mode.model)
						upstream := &httpUpstreamRecorder{resp: &http.Response{
							StatusCode: http.StatusOK,
							Header:     http.Header{"Content-Type": {"text/event-stream"}},
							Body:       io.NopCloser(strings.NewReader("data: {\"type\":\"response.output_text.delta\",\"delta\":\"hello\"}\n\ndata: {\"type\":\"response.completed\",\"response\":" + response + "}\n\n")),
						}}
						svc := &OpenAIGatewayService{cfg: &config.Config{}, httpUpstream: upstream, toolCorrector: NewCodexToolCorrector()}
						account := &Account{
							ID: 91, Platform: PlatformOpenAI, Type: AccountTypeOAuth, Status: StatusActive, Schedulable: true,
							Credentials: map[string]any{"access_token": "test-token", "chatgpt_account_id": "test-account"},
						}
						if endpoint == "passthrough" {
							account.Extra = map[string]any{"openai_passthrough": true}
						}
						c, _ := gin.CreateTestContext(httptest.NewRecorder())
						c.Request = httptest.NewRequest(http.MethodPost, "/v1/responses", bytes.NewReader(body)).WithContext(ctx)
						var result *OpenAIForwardResult
						var err error
						switch endpoint {
						case "chat":
							result, err = svc.ForwardAsChatCompletions(ctx, c, account, body, "", mode.model)
						case "messages":
							result, err = svc.ForwardAsAnthropic(ctx, c, account, body, "", mode.model)
						default:
							result, err = svc.Forward(ctx, c, account, body)
						}
						require.NoError(t, err)
						require.NotNil(t, result)
						wantTier := mode.billed
						wantHint := "model=" + mode.model + ";tier=" + mode.billed
						if standard {
							wantTier, wantHint = "default", "model="+mode.model
						}
						require.Equal(t, wantTier, gjson.GetBytes(upstream.lastBody, "service_tier").String())
						require.Equal(t, wantHint, upstream.lastReq.Header.Get(openAICodexRoutingHintHeader))
						require.Equal(t, mode.billed, optionalStringValue(result.ServiceTier))
						resolution := ApplyOpenAIServiceTierBillingResolution(account, result)
						require.Equal(t, mode.billed, resolution.Billing)
						require.False(t, resolution.Downgraded)
						require.Equal(t, mode.requested, gjson.GetBytes(body, "service_tier").String(), "transport must not mutate caller body")
					})
				}
			}
		}
	}
}

func TestCodexUltrafastUpstreamTier(t *testing.T) {
	account := &Account{Platform: PlatformOpenAI, Type: AccountTypeOAuth}
	for _, model := range []string{"gpt-6.2-sol", "gpt-6.2-astra"} {
		body := []byte(fmt.Sprintf(`{"model":%q,"type":"response.create","service_tier":"ultrafast"}`, model))
		for _, standard := range []bool{false, true} {
			ctx := codexTierGroupContext(standard)
			upstream, err := rewriteCodexFastUpstreamBody(ctx, account, body)
			require.NoError(t, err)
			want := "ultrafast"
			if standard {
				want = "default"
			}
			require.Equal(t, want, gjson.GetBytes(upstream, "service_tier").String())
			require.Equal(t, "ultrafast", gjson.GetBytes(body, "service_tier").String())
			require.Equal(t, "ultrafast", ResolveOpenAIServiceTierBilling(account, "ultrafast", want).Billing)
		}
	}
}

func TestCodexFastUpstreamTier_OnlyTrustedCodexFastRequests(t *testing.T) {
	account := &Account{Platform: PlatformOpenAI, Type: AccountTypeOAuth}
	ctx := codexTierGroupContext(true)
	for _, tier := range []string{"", "default", "auto", "scale", "flex", "ultrafast"} {
		require.Equal(t, tier, codexUpstreamServiceTier(ctx, account, "gpt-6.1-sol", tier))
	}
	for _, tier := range []string{"fast", "priority", " FAST "} {
		require.Equal(t, "default", codexUpstreamServiceTier(ctx, account, "gpt-6.1-sol", tier))
	}
	require.Equal(t, "priority", codexUpstreamServiceTier(context.Background(), account, "gpt-6.1-sol", "priority"))
	require.Equal(t, "priority", codexUpstreamServiceTier(ctx, &Account{Platform: PlatformOpenAI, Type: AccountTypeAPIKey}, "gpt-6.1-sol", "priority"))
	require.Equal(t, "priority", codexUpstreamServiceTier(ctx, nil, "gpt-6.1-sol", "priority"))
	untrusted := context.WithValue(ctx, ctxkey.Group, &Group{ID: 103, OpenAIFastUpstreamStandard: true})
	require.Equal(t, "priority", codexUpstreamServiceTier(untrusted, account, "gpt-6.1-sol", "priority"))
	setup := &Account{Platform: PlatformOpenAI, Type: AccountTypeSetupToken}
	require.Equal(t, "default", codexUpstreamServiceTier(ctx, setup, "gpt-6.1-sol", "priority"))
}

func TestCodexFastUpstreamTier_WebSocketForwarderAndHTTPBridge(t *testing.T) {
	gin.SetMode(gin.TestMode)
	for _, standard := range []bool{false, true} {
		for _, bridge := range []bool{false, true} {
			t.Run(fmt.Sprintf("standard=%t/bridge=%t", standard, bridge), func(t *testing.T) {
				ctx := codexTierGroupContext(standard)
				account := &Account{
					ID: 91, Platform: PlatformOpenAI, Type: AccountTypeOAuth, Status: StatusActive, Schedulable: true, Concurrency: 1,
					Credentials: map[string]any{"access_token": "test-token", "chatgpt_account_id": "test-account"},
					Extra:       map[string]any{"responses_websockets_v2_enabled": true},
				}
				cfg := newOpenAIWSV2TestConfig()
				cfg.Gateway.OpenAIWS.MaxConnsPerAccount = 1
				cfg.Gateway.OpenAIWS.MaxIdlePerAccount = 1
				cfg.Gateway.OpenAIWS.ReadTimeoutSeconds = 3
				cfg.Gateway.OpenAIWS.WriteTimeoutSeconds = 3
				terminal := []byte(`{"type":"response.completed","response":{"id":"resp_ws_tier","model":"gpt-6.1-sol","status":"completed","service_tier":"default","usage":{"input_tokens":10,"output_tokens":2}}}`)
				capture := &openAIWSCaptureConn{events: [][]byte{terminal}}
				dialer := &openAIWSCaptureDialer{conn: capture}
				pool := newOpenAIWSConnPool(cfg)
				defer pool.Close()
				pool.setClientDialerForTest(dialer)
				upstream := &httpUpstreamRecorder{resp: &http.Response{
					StatusCode: http.StatusOK, Header: http.Header{"Content-Type": {"text/event-stream"}},
					Body: io.NopCloser(strings.NewReader("data: " + string(terminal) + "\n\n")),
				}}
				svc := &OpenAIGatewayService{cfg: cfg, httpUpstream: upstream, cache: &stubGatewayCache{}, openaiWSPool: pool, toolCorrector: NewCodexToolCorrector()}
				body := []byte(`{"type":"response.create","model":"gpt-6.1-sol","service_tier":"priority","input":"hi","stream":true}`)
				c, _ := gin.CreateTestContext(httptest.NewRecorder())
				c.Request = httptest.NewRequest(http.MethodGet, "/v1/responses", nil).WithContext(ctx)
				var result *OpenAIForwardResult
				var err error
				var actualTier, actualHint string
				if bridge {
					result, err = svc.proxyOpenAIWSHTTPBridgeTurn(ctx, c, account, "test-token", body, len(body), "gpt-6.1-sol", "", "", "", "", 1, func([]byte) error { return nil })
					if upstream.lastReq != nil {
						actualTier = gjson.GetBytes(upstream.lastBody, "service_tier").String()
						actualHint = upstream.lastReq.Header.Get(openAICodexRoutingHintHeader)
					}
				} else {
					result, err = svc.Forward(ctx, c, account, body)
					require.NoError(t, err)
					require.True(t, result.OpenAIWSMode)
					actualTier, _ = capture.lastWrite["service_tier"].(string)
					actualHint = dialer.lastHeaders.Get(openAICodexRoutingHintHeader)
				}
				require.NoError(t, err)
				wantTier, wantHint := "priority", "model=gpt-6.1-sol;tier=priority"
				if standard {
					wantTier, wantHint = "default", "model=gpt-6.1-sol"
				}
				require.Equal(t, wantTier, actualTier)
				require.Equal(t, wantHint, actualHint)
				require.Equal(t, "priority", optionalStringValue(result.ServiceTier))
				require.Equal(t, "priority", ApplyOpenAIServiceTierBillingResolution(account, result).Billing)
			})
		}
	}
}

func TestCodexFastUpstreamTier_WebSocketTransportPreservesTurnMetadata(t *testing.T) {
	account := &Account{Platform: PlatformOpenAI, Type: AccountTypeOAuth}
	for _, standard := range []bool{false, true} {
		t.Run(fmt.Sprintf("standard=%t", standard), func(t *testing.T) {
			ctx := codexTierGroupContext(standard)
			upstream := newStagedPassthroughConn()
			defer upstream.Close()
			conn := &openAICodexServiceTierFrameConn{FrameConn: upstream, requestCtx: ctx, account: account}
			meta := newOpenAIWSPassthroughUsageMeta("gpt-6.1-sol", nil)
			for _, tier := range []string{"priority", "flex", "", "fast"} {
				body := []byte(`{"type":"response.create","model":"gpt-6.1-sol"}`)
				if tier != "" {
					body = []byte(fmt.Sprintf(`{"type":"response.create","model":"gpt-6.1-sol","service_tier":%q}`, tier))
				}
				meta.updateFromResponseCreate(body, "gpt-6.1-sol", "gpt-6.1-sol")
				before := append([]byte(nil), body...)
				require.NoError(t, conn.WriteFrame(ctx, coderws.MessageText, body))
				actual := requirePassthroughUpstreamWrite(t, upstream, time.Second)
				wantTier := tier
				if standard && (tier == "priority" || tier == "fast") {
					wantTier = "default"
				}
				require.Equal(t, wantTier, gjson.GetBytes(actual, "service_tier").String())
				require.Equal(t, normalizedOpenAIServiceTierValue(tier), optionalStringValue(meta.serviceTier.Load()))
				require.Equal(t, before, body)
			}
			cancel := []byte(`{"type":"response.cancel","service_tier":"priority"}`)
			require.NoError(t, conn.WriteFrame(ctx, coderws.MessageText, cancel))
			require.Equal(t, cancel, requirePassthroughUpstreamWrite(t, upstream, time.Second))
		})
	}
}
