//go:build unit

package service

import (
	"context"
	"io"
	"net/http"
	"testing"

	"github.com/stretchr/testify/require"
)

type rejectedRouteUpstream struct {
	geminiLatencyProfileUpstream
	invalidations int
	proxy         string
	account       int64
}

func (u *rejectedRouteUpstream) InvalidateConnectionPool(proxy string, account int64) {
	u.invalidations++
	u.proxy, u.account = proxy, account
}

func TestAntigravityRejectedRouteUsesOneFreshConnectionOnly(t *testing.T) {
	rejection := []byte(`{"error":{"code":400,"message":"User location is not supported for the API use.","status":"FAILED_PRECONDITION"}}`)
	for _, again := range []bool{false, true} {
		t.Run(map[bool]string{false: "recovers", true: "still_rejected"}[again], func(t *testing.T) {
			secondStatus, secondBody := 200, []byte("ok")
			if again {
				secondStatus, secondBody = 400, rejection
			}
			upstream := &rejectedRouteUpstream{geminiLatencyProfileUpstream: geminiLatencyProfileUpstream{responses: []*http.Response{
				geminiLatencyResponse(400, rejection), geminiLatencyResponse(secondStatus, secondBody),
			}}}
			svc := &AntigravityGatewayService{}
			result, err := svc.antigravityRetryLoop(antigravityRetryLoopParams{
				ctx: context.Background(), account: &Account{ID: 54, Platform: PlatformAntigravity},
				proxyURL: "http://127.0.0.1:17991", accessToken: "test", action: "streamGenerateContent",
				requestedModel: "gemini-3.8-flash", body: []byte(`{"model":"gemini-3.8-flash-low"}`), httpUpstream: upstream,
			})
			require.NoError(t, err)
			require.NotNil(t, result.resp)
			defer func() { _ = result.resp.Body.Close() }()
			require.Equal(t, secondStatus, result.resp.StatusCode)
			body, err := io.ReadAll(result.resp.Body)
			require.NoError(t, err)
			require.Equal(t, secondBody, body)
			require.Len(t, upstream.profiles, 2, "one rejected attempt must never become an unbounded replay")
			require.Equal(t, "http://127.0.0.1:17991", upstream.proxy)
			require.EqualValues(t, 54, upstream.account)
		})
	}
}

func TestAntigravityRejectedRouteLeavesOtherResponsesUntouched(t *testing.T) {
	upstream := &rejectedRouteUpstream{}
	p := antigravityRetryLoopParams{account: &Account{ID: 54}, proxyURL: "http://127.0.0.1:17991", httpUpstream: upstream}
	region := []byte(`{"error":{"message":"User location is not supported for the API use."}}`)
	for _, status := range []int{200, 401, 403, 429, 500} {
		require.False(t, invalidateAntigravityRejectedRoute(p, status, region))
	}
	require.False(t, invalidateAntigravityRejectedRoute(p, 400, []byte(`{"error":{"message":"Invalid tool schema"}}`)))
	p.proxyURL = ""
	require.False(t, invalidateAntigravityRejectedRoute(p, 400, region))
	require.Zero(t, upstream.invalidations)
}
