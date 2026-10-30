package service

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/stretchr/testify/require"
)

func TestSiteFastPricingDoublesEveryTokenComponent(t *testing.T) {
	body, err := os.ReadFile("../../../resources/model-pricing/official-site-prices.json")
	require.NoError(t, err)
	var manifest struct {
		Policy struct {
			Multiplier float64 `json:"multiplier"`
		} `json:"site_fast_policy"`
		Models map[string]struct {
			Pricing ChannelModelPricing `json:"pricing"`
		} `json:"models"`
	}
	require.NoError(t, json.Unmarshal(body, &manifest))
	require.Equal(t, 2.0, manifest.Policy.Multiplier)

	for _, model := range []string{
		"gpt-6-astra", "gpt-6-sol", "gpt-6-luna", "gpt-6.1-sol", "gpt-6.1-terra",
		"gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna",
	} {
		t.Run(model, func(t *testing.T) {
			entry, ok := manifest.Models[model]
			require.True(t, ok)
			require.NotNil(t, entry.Pricing.FastMultiplier)
			require.Equal(t, manifest.Policy.Multiplier, *entry.Pricing.FastMultiplier)
			service := NewBillingService(&config.Config{}, nil)
			resolver := NewModelPricingResolver(nil, service)
			group := &Group{ID: 9, Platform: PlatformComposite, LongContextPricingEnabled: true,
				ModelPricing: []ChannelModelPricing{entry.Pricing}}
			for _, contextTokens := range []int{10000, 272001} {
				for _, rate := range []float64{1, 2} {
					t.Run(fmt.Sprintf("context=%d/group=%g", contextTokens, rate), func(t *testing.T) {
						input := CostInput{
							Ctx: context.Background(), Model: model, Group: group, Resolver: resolver,
							RateMultiplier: rate,
							Tokens: UsageTokens{InputTokens: contextTokens - 3000, CacheReadTokens: 2000,
								CacheCreationTokens: 1000, OutputTokens: 500},
						}
						standard, err := service.CalculateCostUnified(input)
						require.NoError(t, err)
						require.Greater(t, standard.TotalCost, 0.0)
						for tier, multiplier := range map[string]float64{"fast": 2, "priority": 2, "flex": 0.5} {
							input.ServiceTier = tier
							cost, err := service.CalculateCostUnified(input)
							require.NoError(t, err)
							require.InDelta(t, standard.InputCost*multiplier, cost.InputCost, 1e-12, tier)
							require.InDelta(t, standard.OutputCost*multiplier, cost.OutputCost, 1e-12, tier)
							require.InDelta(t, standard.CacheCreationCost*multiplier, cost.CacheCreationCost, 1e-12, tier)
							require.InDelta(t, standard.CacheReadCost*multiplier, cost.CacheReadCost, 1e-12, tier)
							require.InDelta(t, standard.TotalCost*multiplier, cost.TotalCost, 1e-12, tier)
							require.InDelta(t, cost.TotalCost*rate, cost.ActualCost, 1e-12, tier)
						}
					})
				}
			}
		})
	}
}
