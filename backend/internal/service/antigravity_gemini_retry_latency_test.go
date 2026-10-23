//go:build unit

package service

import (
	"context"
	"fmt"
	"io"
	"net/http"
	"strings"
	"testing"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/pkg/tlsfingerprint"
	"github.com/stretchr/testify/require"
)

func geminiLatencyRateBody(model, delay string) []byte {
	retry := ""
	if delay != "missing" {
		retry = fmt.Sprintf(`,{"@type":"type.googleapis.com/google.rpc.RetryInfo","retryDelay":%q}`, delay)
	}
	return []byte(fmt.Sprintf(`{"error":{"status":"RESOURCE_EXHAUSTED","details":[{"@type":"type.googleapis.com/google.rpc.ErrorInfo","reason":"RATE_LIMIT_EXCEEDED","metadata":{"model":%q}}%s]}}`, model, retry))
}

func TestAntigravityGeminiRetryLatencyHints(t *testing.T) {
	account := &Account{ID: 1, Platform: PlatformAntigravity, Type: AccountTypeOAuth}
	for _, test := range []struct {
		name, model, delay string
		parsed, wait       time.Duration
		blocked            bool
	}{
		{"explicit_zero", "gemini-3.8-flash-low", "0s", 0, 100 * time.Millisecond, false},
		{"short_fraction", "gemini-3.8-flash-low", "0.201506475s", 201506475, 201506475, false},
		{"small_fraction", "gemini-3.8-flash-low", "0.005s", 5 * time.Millisecond, 100 * time.Millisecond, false},
		{"long_reset", "gemini-3.8-flash-low", "15s", 15 * time.Second, 15 * time.Second, true},
		{"missing_is_not_zero", "gemini-3.8-flash-low", "missing", 30 * time.Second, 30 * time.Second, true},
		{"invalid_is_not_zero", "gemini-3.8-flash-low", "bad", 30 * time.Second, 30 * time.Second, true},
		{"negative_is_not_zero", "gemini-3.8-flash-low", "-0.5s", 30 * time.Second, 30 * time.Second, true},
		{"claude_keeps_floor", "claude-sonnet-4-6", "0.1s", 100 * time.Millisecond, time.Second, false},
	} {
		t.Run(test.name, func(t *testing.T) {
			body := geminiLatencyRateBody(test.model, test.delay)
			info := parseAntigravitySmartRetryInfo(body)
			require.NotNil(t, info)
			require.Equal(t, test.parsed, info.RetryDelay)
			retry, blocked, wait, model, capacity := shouldTriggerAntigravitySmartRetry(account, body)
			require.Equal(t, !test.blocked, retry)
			require.Equal(t, test.blocked, blocked)
			require.Equal(t, test.wait, wait)
			require.Equal(t, test.model, model)
			require.False(t, capacity)
		})
	}
}

type geminiLatencyProfileUpstream struct {
	profiles  []HTTPUpstreamProfile
	responses []*http.Response
}

func (u *geminiLatencyProfileUpstream) Do(req *http.Request, _ string, _ int64, _ int) (*http.Response, error) {
	u.profiles = append(u.profiles, HTTPUpstreamProfileFromContext(req.Context()))
	response := u.responses[0]
	u.responses = u.responses[1:]
	return response, nil
}

func (u *geminiLatencyProfileUpstream) DoWithTLS(req *http.Request, proxy string, id int64, concurrency int, _ *tlsfingerprint.Profile) (*http.Response, error) {
	return u.Do(req, proxy, id, concurrency)
}

func geminiLatencyResponse(status int, body []byte) *http.Response {
	return &http.Response{StatusCode: status, Header: http.Header{}, Body: io.NopCloser(strings.NewReader(string(body)))}
}

func TestAntigravityGeminiRetryLatencyUsesShortReset(t *testing.T) {
	t.Setenv(antigravityForwardBaseURLEnv, "prod")
	model := "gemini-3.8-flash-low"
	body := geminiLatencyRateBody(model, "0s")
	upstream := &geminiLatencyProfileUpstream{responses: []*http.Response{
		geminiLatencyResponse(429, body), geminiLatencyResponse(200, []byte("ok")),
	}}
	repo := &stubAntigravityAccountRepo{}
	service := &AntigravityGatewayService{}
	started := time.Now()
	result, err := service.antigravityRetryLoop(antigravityRetryLoopParams{
		ctx: context.Background(), account: &Account{ID: 1, Platform: PlatformAntigravity, Concurrency: 67},
		accessToken: "test", action: "streamGenerateContent", requestedModel: "gemini-3.8-flash",
		body: []byte(`{"model":"gemini-3.8-flash-low"}`), httpUpstream: upstream, accountRepo: repo,
	})
	require.NoError(t, err)
	require.NotNil(t, result.resp)
	defer result.resp.Body.Close()
	require.Equal(t, 200, result.resp.StatusCode)
	require.GreaterOrEqual(t, time.Since(started), 100*time.Millisecond)
	require.Less(t, time.Since(started), 750*time.Millisecond, "zero reset must not become a one-second sleep or 30-second account hold")
	require.Equal(t, []HTTPUpstreamProfile{HTTPUpstreamProfileDefault, HTTPUpstreamProfileDefault}, upstream.profiles)
	require.Empty(t, repo.modelRateLimitCalls, "a successful retry must not quarantine the account")
}

func TestAntigravityGeminiRetryLatencyKeepsClaudeTransport(t *testing.T) {
	upstream := &geminiLatencyProfileUpstream{responses: []*http.Response{geminiLatencyResponse(200, []byte("ok"))}}
	service := &AntigravityGatewayService{}
	result, err := service.antigravityRetryLoop(antigravityRetryLoopParams{
		ctx: context.Background(), account: &Account{ID: 1, Platform: PlatformAntigravity},
		accessToken: "test", action: "streamGenerateContent", requestedModel: "claude-sonnet-4-6",
		body: []byte(`{}`), httpUpstream: upstream,
	})
	require.NoError(t, err)
	defer result.resp.Body.Close()
	require.Equal(t, []HTTPUpstreamProfile{HTTPUpstreamProfileDefault}, upstream.profiles)
}

func TestAntigravityGeminiRetryLatencyKeepsAccountAcrossShortWindow(t *testing.T) {
	initial := geminiLatencyRateBody("gemini-3.8-flash-low", "0.1s")
	reset := geminiLatencyRateBody("gemini-3.8-flash-low", "0s")
	upstream := &geminiLatencyProfileUpstream{responses: []*http.Response{
		geminiLatencyResponse(429, reset), geminiLatencyResponse(200, []byte("ok")),
	}}
	repo := &stubAntigravityAccountRepo{}
	service := &AntigravityGatewayService{}
	started := time.Now()
	result := service.handleSmartRetry(antigravityRetryLoopParams{
		ctx: context.Background(), account: &Account{ID: 1, Platform: PlatformAntigravity},
		accessToken: "test", action: "streamGenerateContent", requestedModel: "gemini-3.8-flash",
		body: []byte(`{}`), httpUpstream: upstream, accountRepo: repo,
	}, geminiLatencyResponse(429, initial), initial, "https://example.invalid", 0, nil)
	require.Nil(t, result.switchError)
	require.Equal(t, 200, result.resp.StatusCode)
	defer result.resp.Body.Close()
	require.Len(t, upstream.profiles, 2)
	require.Less(t, time.Since(started), 750*time.Millisecond)
	require.Empty(t, repo.modelRateLimitCalls)
}

func TestAntigravityGeminiRetryLatencyUsesLatestCooldown(t *testing.T) {
	for _, delay := range []string{"0s", "15s"} {
		t.Run(delay, func(t *testing.T) {
			initial := geminiLatencyRateBody("gemini-3.8-flash-low", "0s")
			last := geminiLatencyRateBody("gemini-3.8-flash-low", delay)
			upstream := &geminiLatencyProfileUpstream{responses: []*http.Response{
				geminiLatencyResponse(429, last), geminiLatencyResponse(429, last), geminiLatencyResponse(429, last),
			}}
			repo := &stubAntigravityAccountRepo{}
			service := &AntigravityGatewayService{}
			result := service.handleSmartRetry(antigravityRetryLoopParams{
				ctx: context.Background(), account: &Account{ID: 1, Platform: PlatformAntigravity},
				accessToken: "test", action: "streamGenerateContent", requestedModel: "gemini-3.8-flash",
				body: []byte(`{}`), httpUpstream: upstream, accountRepo: repo,
			}, geminiLatencyResponse(429, initial), initial, "https://example.invalid", 0, nil)
			require.NotNil(t, result.switchError)
			require.Len(t, repo.modelRateLimitCalls, 2)
			remaining := time.Until(repo.modelRateLimitCalls[0].resetAt)
			if delay == "0s" {
				require.Greater(t, remaining, 0*time.Second)
				require.LessOrEqual(t, remaining, 100*time.Millisecond)
			} else {
				require.Greater(t, remaining, 14*time.Second)
				require.LessOrEqual(t, remaining, 15*time.Second)
			}
		})
	}
}

func TestAntigravityGeminiRetryLatencyCancellation(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	body := geminiLatencyRateBody("gemini-3.8-flash-low", "0s")
	upstream := &geminiLatencyProfileUpstream{}
	service := &AntigravityGatewayService{}
	result := service.handleSmartRetry(antigravityRetryLoopParams{
		ctx: ctx, account: &Account{ID: 1, Platform: PlatformAntigravity}, httpUpstream: upstream,
	}, geminiLatencyResponse(429, body), body, "https://example.invalid", 0, nil)
	require.ErrorIs(t, result.err, context.Canceled)
	require.Empty(t, upstream.profiles)
}

func TestAntigravityGeminiRetryLatencyTotalSleepBudget(t *testing.T) {
	initial := geminiLatencyRateBody("gemini-3.8-flash-low", "0.45s")
	last := geminiLatencyRateBody("gemini-3.8-flash-low", "0.75s")
	upstream := &geminiLatencyProfileUpstream{responses: []*http.Response{
		geminiLatencyResponse(429, last), geminiLatencyResponse(429, last), geminiLatencyResponse(429, last),
	}}
	service := &AntigravityGatewayService{}
	repo := &stubAntigravityAccountRepo{}
	started := time.Now()
	result := service.handleSmartRetry(antigravityRetryLoopParams{
		ctx: context.Background(), account: &Account{ID: 1, Platform: PlatformAntigravity},
		accessToken: "test", action: "streamGenerateContent", requestedModel: "gemini-3.8-flash",
		body: []byte(`{}`), httpUpstream: upstream, accountRepo: repo,
	}, geminiLatencyResponse(429, initial), initial, "https://example.invalid", 0, nil)
	require.NotNil(t, result.switchError)
	require.Len(t, upstream.profiles, 2, "a third 750ms sleep exceeds the 1.5s budget")
	require.Less(t, time.Since(started), 1700*time.Millisecond)
}

func TestAntigravityGeminiRetryLatencyRechecksShrinkingReset(t *testing.T) {
	for _, test := range []struct {
		name, delay string
		wantSuccess bool
	}{
		{"shrinks_to_zero", "0s", true},
		{"becomes_long_hold", "15s", false},
		{"remains_longer_window", "0.6s", false},
	} {
		t.Run(test.name, func(t *testing.T) {
			initial := geminiLatencyRateBody("gemini-3.8-flash-low", "0.6s")
			latest := geminiLatencyRateBody("gemini-3.8-flash-low", test.delay)
			upstream := &geminiLatencyProfileUpstream{responses: []*http.Response{
				geminiLatencyResponse(429, latest), geminiLatencyResponse(200, []byte("ok")),
			}}
			repo := &stubAntigravityAccountRepo{}
			service := &AntigravityGatewayService{}
			result := service.handleSmartRetry(antigravityRetryLoopParams{
				ctx: context.Background(), account: &Account{ID: 1, Platform: PlatformAntigravity},
				accessToken: "test", action: "streamGenerateContent", requestedModel: "gemini-3.8-flash",
				body: []byte(`{}`), httpUpstream: upstream, accountRepo: repo,
			}, geminiLatencyResponse(429, initial), initial, "https://example.invalid", 0, nil)
			if test.wantSuccess {
				require.Nil(t, result.switchError)
				require.Equal(t, 200, result.resp.StatusCode)
				defer result.resp.Body.Close()
				require.Len(t, upstream.profiles, 2)
				require.Empty(t, repo.modelRateLimitCalls)
			} else {
				require.NotNil(t, result.switchError)
				require.Len(t, upstream.profiles, 1)
				require.NotEmpty(t, repo.modelRateLimitCalls)
				if test.delay == "15s" {
					require.Greater(t, time.Until(repo.modelRateLimitCalls[0].resetAt), 14*time.Second)
				}
			}
		})
	}
}
