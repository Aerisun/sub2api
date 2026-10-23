package service

import (
	"context"
	"net/http"
	"testing"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/stretchr/testify/require"
)

type transientCooldownAccountRepo struct {
	AccountRepository
}

func (transientCooldownAccountRepo) SetOverloaded(context.Context, int64, time.Time) error {
	return nil
}

func TestHandleOpenAITransientError_BlocksOnlyRequestedModel(t *testing.T) {
	svc := &OpenAIGatewayService{}
	svc.rateLimitService = NewRateLimitService(transientCooldownAccountRepo{}, nil, &config.Config{}, nil, nil)
	account := &Account{
		ID:       5105,
		Platform: PlatformOpenAI,
		Type:     AccountTypeAPIKey,
	}

	firstShouldDisable := svc.handleOpenAIAccountUpstreamError(context.Background(), account, http.StatusBadGateway, http.Header{}, []byte(`{"error":{"message":"Upstream request failed","type":"upstream_error"}}`), "gpt-5.5")
	secondShouldDisable := svc.handleOpenAIAccountUpstreamError(context.Background(), account, http.StatusBadGateway, http.Header{}, []byte(`{"error":{"message":"Upstream request failed","type":"upstream_error"}}`), "gpt-5.5")

	require.False(t, firstShouldDisable)
	require.False(t, secondShouldDisable)
	require.False(t, svc.isOpenAIAccountRuntimeBlocked(account))
	require.True(t, svc.isOpenAIAccountModelRuntimeBlocked(account, "gpt-5.5"))
	require.False(t, svc.isOpenAIAccountModelRuntimeBlocked(account, "gpt-5.6-terra"))
}

func TestHandleOpenAITransientError_LocalManagedPoolKeepsHealthyMembersSchedulable(t *testing.T) {
	for _, status := range []int{429, 500, 502, 503, 504} {
		t.Run(http.StatusText(status), func(t *testing.T) {
			svc := &OpenAIGatewayService{}
			svc.rateLimitService = NewRateLimitService(transientCooldownAccountRepo{}, nil, &config.Config{}, nil, nil)
			account := &Account{ID: 66, Platform: PlatformOpenAI, Type: AccountTypeAPIKey,
				Credentials: map[string]any{"base_url": "http://127.0.0.1:8096", "pool_mode": true,
					"pool_manages_transient_errors": true, "pool_mode_retry_count": 0,
					"pool_mode_retry_status_codes": []any{}}}
			for range 4 {
				require.False(t, svc.handleOpenAIAccountUpstreamError(context.Background(), account, status,
					http.Header{"Retry-After": {"3"}}, []byte(`{"error":{"message":"temporary pool error"}}`), "glm-flash"))
			}
			require.False(t, svc.isOpenAIAccountRuntimeBlocked(account))
			require.False(t, svc.isOpenAIAccountModelRuntimeBlocked(account, "glm-flash"))
			require.Equal(t, 0, account.GetPoolModeRetryCount(), "delegation must not enable replay")
		})
	}
}

func TestManagedPoolTransientErrors_RequiresExplicitLocalPoolOptIn(t *testing.T) {
	for _, base := range []string{"https://provider.example", "http://provider.example", "http://127.0.0.1:8096"} {
		account := &Account{Platform: PlatformOpenAI, Type: AccountTypeAPIKey,
			Credentials: map[string]any{"base_url": base, "pool_mode": true}}
		require.False(t, poolManagesTransientErrors(account))
		account.Credentials["pool_manages_transient_errors"] = true
		require.Equal(t, base == "http://127.0.0.1:8096", poolManagesTransientErrors(account))
		require.Equal(t, base != "http://127.0.0.1:8096", isOpenAIAPIKeyHealthBreakerAccount(account))
		account.Credentials["pool_mode"] = false
		require.False(t, poolManagesTransientErrors(account))
	}
}

func TestHandleOpenAITransientError_TransientStatusesUseModelScope(t *testing.T) {
	for _, statusCode := range []int{http.StatusInternalServerError, http.StatusBadGateway, http.StatusServiceUnavailable, http.StatusGatewayTimeout, 520, 521, 522, 523, 524} {
		t.Run(http.StatusText(statusCode), func(t *testing.T) {
			svc := &OpenAIGatewayService{}
			svc.rateLimitService = NewRateLimitService(transientCooldownAccountRepo{}, nil, &config.Config{}, nil, nil)
			account := &Account{
				ID:       int64(5100 + statusCode),
				Platform: PlatformOpenAI,
				Type:     AccountTypeAPIKey,
			}

			firstShouldDisable := svc.handleOpenAIAccountUpstreamError(context.Background(), account, statusCode, http.Header{}, []byte(`{"error":{"message":"temporary upstream failure"}}`), "gpt-5.5")
			secondShouldDisable := svc.handleOpenAIAccountUpstreamError(context.Background(), account, statusCode, http.Header{}, []byte(`{"error":{"message":"temporary upstream failure"}}`), "gpt-5.5")

			require.False(t, firstShouldDisable)
			require.False(t, secondShouldDisable)
			require.False(t, svc.isOpenAIAccountRuntimeBlocked(account), "status %d must not block the whole account", statusCode)
			require.True(t, svc.isOpenAIAccountModelRuntimeBlocked(account, "gpt-5.5"), "status %d should block the failing model", statusCode)
		})
	}
}

func TestHandleOpenAITransientError_529RemainsOverloadOnly(t *testing.T) {
	require.False(t, shouldCooldownOpenAITransientUpstreamError(529, []byte(`{"error":{"message":"overloaded"}}`)))
}

func TestHandleOpenAITransientError_CanonicalModelIsNotMappedTwice(t *testing.T) {
	svc := &OpenAIGatewayService{}
	svc.rateLimitService = NewRateLimitService(transientCooldownAccountRepo{}, nil, &config.Config{}, nil, nil)
	account := &Account{
		ID:       5107,
		Platform: PlatformOpenAI,
		Type:     AccountTypeAPIKey,
		Credentials: map[string]any{
			"model_mapping": map[string]any{
				"public-alias": "upstream-a",
				"upstream-a":   "upstream-b",
			},
		},
	}
	canonicalModel := account.GetMappedModel("public-alias")
	require.Equal(t, "upstream-a", canonicalModel)

	for range 2 {
		svc.handleOpenAIAccountUpstreamError(context.Background(), account, http.StatusBadGateway, http.Header{}, []byte(`{"error":{"message":"temporary upstream failure"}}`), canonicalModel)
	}

	require.True(t, svc.isOpenAIAccountModelRuntimeBlocked(account, "public-alias"))
	svc.ReportOpenAIAccountScheduleResult(account, canonicalModel, true, nil)
	require.False(t, svc.isOpenAIAccountModelRuntimeBlocked(account, "public-alias"))
}

func TestHandleOpenAITransientError_DoesNotBlockParameter400(t *testing.T) {
	svc := &OpenAIGatewayService{}
	svc.rateLimitService = NewRateLimitService(transientCooldownAccountRepo{}, nil, &config.Config{}, nil, nil)
	account := &Account{
		ID:       5103,
		Platform: PlatformOpenAI,
		Type:     AccountTypeAPIKey,
	}

	shouldDisable := svc.handleOpenAIAccountUpstreamError(context.Background(), account, http.StatusBadRequest, http.Header{}, []byte(`{"error":{"message":"Invalid type for input[0].arguments"}}`), "gpt-5.5")

	require.False(t, shouldDisable)
	require.False(t, svc.isOpenAIAccountRuntimeBlocked(account))
	require.False(t, svc.isOpenAIAccountModelRuntimeBlocked(account, "gpt-5.5"))
}

func TestHandleOpenAITransientError_HardDisableStillBlocksWholeAccount(t *testing.T) {
	svc := &OpenAIGatewayService{}
	account := &Account{ID: 5106, Platform: PlatformOpenAI, Type: AccountTypeAPIKey}

	svc.BlockAccountScheduling(account, time.Now().Add(time.Minute), "upstream_disable")

	require.True(t, svc.isOpenAIAccountRuntimeBlocked(account))
	require.False(t, svc.isOpenAIAccountRequestRuntimeBlocked(account, "gpt-5.5"))
	require.False(t, svc.isOpenAIAccountRequestRuntimeBlocked(account, "gpt-5.6-sol"))
	require.False(t, svc.isOpenAIAccountRuntimeBlocked(account))
}
