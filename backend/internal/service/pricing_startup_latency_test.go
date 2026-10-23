package service

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"os"
	"path/filepath"
	"sync"
	"testing"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/stretchr/testify/require"
)

type blockedStartupPricingClient struct {
	started chan struct{}
	release chan struct{}
	once    sync.Once
	hash    string
}

func (c *blockedStartupPricingClient) FetchHashText(ctx context.Context, _ string) (string, error) {
	c.once.Do(func() { close(c.started) })
	select {
	case <-c.release:
		return c.hash, nil
	case <-ctx.Done():
		return "", ctx.Err()
	}
}
func (c *blockedStartupPricingClient) FetchPricingJSON(context.Context, string) ([]byte, error) {
	return nil, nil // The equal hash must never download or replace this catalog.
}

func TestPricingWarmStartupDoesNotWaitForRemoteAndPreservesOverrides(t *testing.T) {
	dir := t.TempDir()
	body := []byte(`{"glm-5.3":{"input_cost_per_token":0.000001,"output_cost_per_token":0.000002}}`)
	require.NoError(t, os.WriteFile(filepath.Join(dir, "model_pricing.json"), body, 0600))
	override := filepath.Join(dir, "override.json")
	require.NoError(t, os.WriteFile(override, []byte(`{"glm-5.3":{"input_cost_per_token":0.000003,"output_cost_per_token":0.000004}}`), 0600))
	hash := sha256.Sum256(body)
	client := &blockedStartupPricingClient{started: make(chan struct{}), release: make(chan struct{}), hash: hex.EncodeToString(hash[:])}
	svc := NewPricingService(&config.Config{Pricing: config.PricingConfig{
		DataDir: dir, RemoteURL: "https://raw.githubusercontent.com/example/prices/main/prices.json",
		HashURL:      "https://raw.githubusercontent.com/example/prices/main/prices.sha256",
		OverrideFile: override, HashCheckIntervalMinutes: 10,
	}}, client)
	defer func() { close(client.release); svc.Stop() }()
	began := time.Now()
	require.NoError(t, svc.Initialize())
	require.Less(t, time.Since(began), 100*time.Millisecond)
	select {
	case <-client.started:
	case <-time.After(time.Second):
		t.Fatal("remote freshness check was dropped instead of running in the background")
	}
	pricing, found := svc.pricingData["glm-5.3"]
	require.True(t, found)
	require.Equal(t, 0.000003, pricing.InputCostPerToken)
	require.Equal(t, 0.000004, pricing.OutputCostPerToken)
}

func TestPricingInvalidLocalAndMissingFallbackStillFailsClosed(t *testing.T) {
	dir := t.TempDir()
	require.NoError(t, os.WriteFile(filepath.Join(dir, "model_pricing.json"), []byte("broken"), 0600))
	svc := NewPricingService(&config.Config{Pricing: config.PricingConfig{
		DataDir: dir, FallbackFile: filepath.Join(dir, "absent.json"),
	}}, nil)
	require.Error(t, svc.Initialize())
}
