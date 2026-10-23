//go:build unit

package service

import (
	"context"
	"fmt"
	"testing"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/Wei-Shaw/sub2api/internal/pkg/ctxkey"
	"github.com/stretchr/testify/require"
)

func TestGeminiConversationAcrossProtocols(t *testing.T) {
	svc := &GatewayService{}
	for _, protocol := range []string{"anthropic", "chat_completions", "responses", "gemini"} {
		t.Run(protocol, func(t *testing.T) {
			first, second := `{"messages":[{"role":"user","content":"hello"}]}`, `{"messages":[{"role":"user","content":"hello"},{"role":"assistant","content":"hi"},{"role":"user","content":"next"}]}`
			if protocol == "responses" {
				first, second = `{"input":[{"role":"user","content":"hello"}]}`, `{"input":[{"role":"user","content":"hello"},{"role":"assistant","content":"hi"},{"role":"user","content":"next"}]}`
			} else if protocol == "gemini" {
				first, second = `{"contents":[{"role":"user","parts":[{"text":"hello"}]}]}`, `{"contents":[{"role":"user","parts":[{"text":"hello"}]},{"role":"model","parts":[{"text":"hi"}]},{"role":"user","parts":[{"text":"next"}]}]}`
			}
			one, err := ParseGatewayRequest(NewRequestBodyRef([]byte(first)), protocol)
			require.NoError(t, err)
			two, err := ParseGatewayRequest(NewRequestBodyRef([]byte(second)), protocol)
			require.NoError(t, err)
			one.SessionContext, two.SessionContext = &SessionContext{APIKeyID: 9}, &SessionContext{APIKeyID: 9}
			for _, id := range []string{"", "conversation-a"} {
				hash := svc.GenerateConversationHash(one, "gemini-3.8-flash", id)
				require.NotEmpty(t, hash)
				require.Equal(t, hash, svc.GenerateConversationHash(two, "gemini-3.8-flash", id))
				two.SessionContext.APIKeyID = 10
				require.NotEqual(t, hash, svc.GenerateConversationHash(two, "gemini-3.8-flash", id))
				two.SessionContext.APIKeyID = 9
			}
			require.NotEqual(t, svc.GenerateConversationHash(one, "gemini-3.8-flash", "a"), svc.GenerateConversationHash(one, "gemini-3.8-flash", "b"))
		})
	}
}

func TestGeminiConversationSignalsAndSharedCachePrefix(t *testing.T) {
	svc := &GatewayService{}
	for _, field := range []string{`"conversation_id":"a"`, `"metadata":{"session_id":"a"}`, `"metadata":{"conversation_id":"a"}`, `"session_id":"a"`, `"prompt_cache_key":"a"`} {
		p := mustParseSessionHashRequest(t, `{`+field+`,"messages":[{"role":"user","content":"hello"}]}`, &SessionContext{APIKeyID: 1})
		require.Equal(t, svc.GenerateConversationHash(p, "gemini-3.8-flash", ""), svc.GenerateConversationHash(p, "gemini-3.8-flash", "a"))
		require.Equal(t, "a", GeminiConversationID(p, ""))
	}
	one := mustParseSessionHashRequest(t, `{"system":[{"type":"text","text":"shared system","cache_control":{"type":"ephemeral"}}],"messages":[{"role":"user","content":"task a"}]}`, nil)
	two := mustParseSessionHashRequest(t, `{"system":[{"type":"text","text":"shared system","cache_control":{"type":"ephemeral"}}],"messages":[{"role":"user","content":"task b"}]}`, nil)
	require.NotEqual(t, svc.GenerateConversationHash(one, "gemini-3.8-flash", ""), svc.GenerateConversationHash(two, "gemini-3.8-flash", ""))
	require.Equal(t, svc.GenerateSessionHash(one), svc.GenerateConversationHash(one, "claude-sonnet-4-6", "new-header"))
	require.Equal(t, svc.GenerateSessionHash(one), svc.GenerateConversationHash(one, "glm-5.3", "new-header"))
	imageOne := mustParseSessionHashRequest(t, `{"messages":[{"role":"user","content":[{"type":"image","source":{"data":"AAA"}}]}]}`, nil)
	imageTwo := mustParseSessionHashRequest(t, `{"messages":[{"role":"user","content":[{"type":"image","source":{"data":"BBB"}}]}]}`, nil)
	require.NotEqual(t, svc.GenerateConversationHash(imageOne, "gemini-3.8-flash", ""), svc.GenerateConversationHash(imageTwo, "gemini-3.8-flash", ""))
	require.Empty(t, svc.GenerateConversationHash(nil, "gemini-3.8-flash", ""))
	first := mustParseResponsesSessionHashRequest(t, `{"input":"hello"}`, nil)
	next := mustParseResponsesSessionHashRequest(t, `{"input":[{"role":"user","content":[{"type":"input_text","text":"hello"}]},{"role":"assistant","content":"hi"},{"role":"user","content":"next"}]}`, nil)
	require.Equal(t, svc.GenerateConversationHash(first, "gemini-3.8-flash", ""), svc.GenerateConversationHash(next, "gemini-3.8-flash", ""))
}

type geminiSlotsCache struct {
	mockConcurrencyCache
	active map[int64]int
}

func (c *geminiSlotsCache) AcquireAccountSlot(_ context.Context, id int64, limit int, _ string) (bool, error) {
	if c.active[id] >= limit {
		return false, nil
	}
	c.active[id]++
	return true, nil
}

func (c *geminiSlotsCache) ReleaseAccountSlot(_ context.Context, id int64, _ string) error {
	c.active[id]--
	return nil
}

func (c *geminiSlotsCache) GetAccountsLoadBatch(_ context.Context, accounts []AccountWithConcurrency) (map[int64]*AccountLoadInfo, error) {
	result := make(map[int64]*AccountLoadInfo)
	for _, a := range accounts {
		result[a.ID] = &AccountLoadInfo{AccountID: a.ID, CurrentConcurrency: c.active[a.ID], LoadRate: c.active[a.ID] * 100 / a.MaxConcurrency}
	}
	return result, nil
}

func geminiSchedulerFixture(routed bool) (*GatewayService, context.Context, *geminiSlotsCache, *mockGatewayCacheForPlatform) {
	group := &Group{ID: 9, Platform: PlatformAntigravity, Status: StatusActive, Hydrated: true, ModelRoutingEnabled: routed}
	if routed {
		group.ModelRouting = map[string][]int64{"gemini-3.8-flash": {1, 3, 35}}
	}
	repo := &mockAccountRepoForPlatform{accountsByID: map[int64]*Account{}}
	for i, id := range []int64{1, 3, 35} {
		concurrency := 32
		if id == 35 {
			concurrency = 10
		}
		// Distinct asynchronous last_used_at values must not create a hotspot.
		lastUsed := time.Now().Add(time.Duration(i) * time.Minute)
		repo.accounts = append(repo.accounts, Account{ID: id, Platform: PlatformAntigravity, Type: AccountTypeOAuth, Status: StatusActive,
			Schedulable: true, Priority: 1, Concurrency: concurrency, LastUsedAt: &lastUsed,
			Credentials: map[string]any{"antigravity_strict_model_mapping": true, "model_mapping": map[string]any{"gemini-3.8-flash": "gemini-3.8-flash-low"}}})
	}
	for i := range repo.accounts {
		repo.accountsByID[repo.accounts[i].ID] = &repo.accounts[i]
	}
	slots := &geminiSlotsCache{active: map[int64]int{}}
	cache := &mockGatewayCacheForPlatform{sessionBindings: map[string]int64{}}
	cfg := testConfig()
	cfg.Gateway.Scheduling = config.GatewaySchedulingConfig{LoadBatchEnabled: true, StickySessionWaitTimeout: 120 * time.Second, StickySessionMaxWaiting: 3, FallbackWaitTimeout: 30 * time.Second, FallbackMaxWaiting: 100}
	svc := &GatewayService{accountRepo: repo, groupRepo: &mockGroupRepoForGateway{groups: map[int64]*Group{9: group}}, cache: cache, cfg: cfg, concurrencyService: NewConcurrencyService(slots)}
	return svc, context.WithValue(context.Background(), ctxkey.Group, group), slots, cache
}

func TestGeminiSchedulerConversationDistributionAndSticky(t *testing.T) {
	for _, routed := range []bool{false, true} {
		t.Run(fmt.Sprint("routing=", routed), func(t *testing.T) {
			svc, ctx, _, _ := geminiSchedulerFixture(routed)
			groupID := int64(9)
			counts := map[int64]int{}
			for i := range 1000 {
				session := fmt.Sprint("conversation-", i)
				one, err := svc.SelectAccountWithLoadAwareness(ctx, &groupID, session, "gemini-3.8-flash", nil, "", 0)
				require.NoError(t, err)
				require.True(t, one.Acquired)
				counts[one.Account.ID]++
				one.ReleaseFunc()
				two, err := svc.SelectAccountWithLoadAwareness(ctx, &groupID, session, "gemini-3.8-flash", nil, "", 0)
				require.NoError(t, err)
				require.Equal(t, one.Account.ID, two.Account.ID)
				two.ReleaseFunc()
			}
			require.InDelta(t, 432, counts[1], 60)
			require.InDelta(t, 432, counts[3], 60)
			require.InDelta(t, 135, counts[35], 50)
		})
	}
}

func TestGeminiSchedulerBusyStickyAndFullPool(t *testing.T) {
	for _, routed := range []bool{false, true} {
		t.Run(fmt.Sprint("routing=", routed), func(t *testing.T) {
			svc, ctx, slots, cache := geminiSchedulerFixture(routed)
			groupID := int64(9)
			cache.sessionBindings["existing"] = 1
			slots.active[1] = 32
			selected, err := svc.SelectAccountWithLoadAwareness(ctx, &groupID, "existing", "gemini-3.8-flash", nil, "", 0)
			require.NoError(t, err)
			require.True(t, selected.Acquired, "an idle alternative must be acquired before queuing")
			require.NotEqual(t, int64(1), selected.Account.ID)
			selected.ReleaseFunc()
			slots.active[1] = 0
			var releases []func()
			for i := range 100 {
				result, err := svc.SelectAccountWithLoadAwareness(ctx, &groupID, fmt.Sprint("outstanding-", i), "gemini-3.8-flash", nil, "", 0)
				require.NoError(t, err)
				if i < 74 {
					require.True(t, result.Acquired)
					releases = append(releases, result.ReleaseFunc)
				} else {
					require.False(t, result.Acquired)
					require.LessOrEqual(t, result.WaitPlan.Timeout, 3*time.Second)
				}
			}
			require.Equal(t, map[int64]int{1: 32, 3: 32, 35: 10}, slots.active)
			for _, release := range releases {
				release()
			}
			require.Equal(t, map[int64]int{1: 0, 3: 0, 35: 0}, slots.active)
		})
	}
}
