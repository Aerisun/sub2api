//go:build unit

package service

import (
	"context"
	"testing"
	"time"

	"github.com/stretchr/testify/require"
)

type poolWaitCache struct {
	mockConcurrencyCache
	loads int
}

func (c *poolWaitCache) GetAccountsLoadBatch(_ context.Context, accounts []AccountWithConcurrency) (map[int64]*AccountLoadInfo, error) {
	c.loads++
	out := map[int64]*AccountLoadInfo{}
	for _, account := range accounts {
		load := 100
		if c.loads >= 2 && account.ID == 1 {
			load = 0
		}
		out[account.ID] = &AccountLoadInfo{AccountID: account.ID, LoadRate: load}
	}
	return out, nil
}

func TestGatewayPoolWaitReconsidersIdleAccountAfterFailover(t *testing.T) {
	svc, ctx, _, _ := geminiSchedulerFixture(false)
	cache := &poolWaitCache{}
	svc.concurrencyService = NewConcurrencyService(cache)
	groupID := int64(9)
	excluded := map[int64]struct{}{35: {}}
	started := time.Now()
	result, err := svc.SelectAccountWithPoolWait(ctx, &groupID, "failed-over-conversation", "gemini-3.8-flash", excluded, "", 0)
	require.NoError(t, err)
	require.True(t, result.Acquired)
	require.Equal(t, int64(1), result.Account.ID)
	require.GreaterOrEqual(t, cache.loads, 2)
	require.Less(t, time.Since(started), 500*time.Millisecond)
	require.Equal(t, map[int64]struct{}{35: {}}, excluded, "slot polling must never reset upstream failure exclusions")
	result.ReleaseFunc()
}

func TestGatewayPoolWaitRemainsBoundedAndCancelable(t *testing.T) {
	for _, cancel := range []bool{false, true} {
		t.Run(map[bool]string{false: "timeout", true: "cancel"}[cancel], func(t *testing.T) {
			svc, ctx, _, _ := geminiSchedulerFixture(false)
			svc.cfg.Gateway.Scheduling.FallbackWaitTimeout = 60 * time.Millisecond
			svc.concurrencyService = NewConcurrencyService(&mockConcurrencyCache{acquireResults: map[int64]bool{1: false, 3: false, 35: false},
				loadMap: map[int64]*AccountLoadInfo{1: {LoadRate: 100}, 3: {LoadRate: 100}, 35: {LoadRate: 100}}})
			if cancel {
				var stop context.CancelFunc
				ctx, stop = context.WithCancel(ctx)
				stop()
			}
			started := time.Now()
			groupID := int64(9)
			result, err := svc.SelectAccountWithPoolWait(ctx, &groupID, "busy", "gemini-3.8-flash", nil, "", 0)
			if cancel {
				require.ErrorIs(t, err, context.Canceled)
			} else {
				require.NoError(t, err)
				require.False(t, result.Acquired)
				require.Equal(t, time.Millisecond, result.WaitPlan.Timeout)
			}
			require.Less(t, time.Since(started), 250*time.Millisecond)
		})
	}
}

func TestGatewayPoolWaitKeepsHealthyStickyAccount(t *testing.T) {
	svc, ctx, _, cache := geminiSchedulerFixture(false)
	cache.sessionBindings["cached"] = 3
	groupID := int64(9)
	result, err := svc.SelectAccountWithPoolWait(ctx, &groupID, "cached", "gemini-3.8-flash", nil, "", 0)
	require.NoError(t, err)
	require.True(t, result.Acquired)
	require.Equal(t, int64(3), result.Account.ID)
	result.ReleaseFunc()
}

func TestGatewayPoolWaitShortModelReset(t *testing.T) {
	for _, model := range []string{"claude-sonnet-4-6", "gemini-3.8-flash"} {
		t.Run(model, func(t *testing.T) {
			svc, ctx, _, _ := geminiSchedulerFixture(false)
			repo := svc.accountRepo.(*mockAccountRepoForPlatform)
			reset := time.Now().Add(220 * time.Millisecond).Format(time.RFC3339Nano)
			for i := range repo.accounts {
				repo.accounts[i].Credentials["model_mapping"] = map[string]any{model: model}
				repo.accounts[i].Extra = map[string]any{modelRateLimitsKey: map[string]any{model: map[string]any{"rate_limit_reset_at": reset}}}
			}
			groupID := int64(9)
			started := time.Now()
			result, err := svc.SelectAccountWithPoolWait(ctx, &groupID, "new-during-reset", model, nil, "", 0)
			require.NoError(t, err)
			require.True(t, result.Acquired)
			require.GreaterOrEqual(t, time.Since(started), 150*time.Millisecond)
			require.Less(t, time.Since(started), time.Second)
			result.ReleaseFunc()
		})
	}
}

func TestGatewayPoolWaitRejectsLongQuotaAndMissingModelsImmediately(t *testing.T) {
	svc, ctx, _, _ := geminiSchedulerFixture(false)
	repo := svc.accountRepo.(*mockAccountRepoForPlatform)
	for i := range repo.accounts {
		repo.accounts[i].Extra = map[string]any{modelRateLimitsKey: map[string]any{"gemini-3.8-flash-low": map[string]any{
			"rate_limit_reset_at": time.Now().Add(time.Hour).Format(time.RFC3339)}}}
	}
	groupID := int64(9)
	for _, model := range []string{"gemini-3.8-flash", "gemini-missing"} {
		started := time.Now()
		_, err := svc.SelectAccountWithPoolWait(ctx, &groupID, "invalid", model, nil, "", 0)
		require.ErrorIs(t, err, ErrNoAvailableAccounts)
		require.Less(t, time.Since(started), 200*time.Millisecond)
	}
}
