package service

import (
	"context"
	"encoding/json"
	"testing"

	"github.com/stretchr/testify/require"
)

func TestAPIKeyAllowedModelsNormalization(t *testing.T) {
	models, err := normalizeAPIKeyAllowedModels([]string{" claude-sonnet-4-6 ", "CLAUDE-SONNET-4-6", "gemini-3.8-flash"})
	require.NoError(t, err)
	require.Equal(t, []string{"claude-sonnet-4-6", "gemini-3.8-flash"}, models)
	for _, invalid := range [][]string{{""}, {"   "}, {"*"}, {"gemini-*"}, {"model\nname"}, {"model\x00name"}, make([]string, 201)} {
		_, err := normalizeAPIKeyAllowedModels(invalid)
		require.Error(t, err)
	}
	models, err = normalizeAPIKeyAllowedModels(nil)
	require.NoError(t, err)
	require.Empty(t, models)
}

func TestAPIKeyAllowedModelsIntersectionAndCacheIsolation(t *testing.T) {
	original := &APIKey{AllowedModels: []string{"claude-sonnet-4-6", "gemini-3.8-flash"}, Group: &Group{ModelAllowlist: GroupModelAllowlist{Enabled: true, Models: []string{"claude-*"}}}}
	restricted := original.WithModelRestrictions()
	require.NotSame(t, original, restricted)
	require.NotSame(t, original.Group, restricted.Group)
	require.True(t, restricted.Group.ModelAllowlist.Allows("claude-sonnet-4-6-thinking"))
	require.False(t, restricted.Group.ModelAllowlist.Allows("claude-opus-4-6"))
	require.False(t, restricted.Group.ModelAllowlist.Allows("gemini-3.8-flash"))
	require.Equal(t, []string{"claude-*"}, original.Group.ModelAllowlist.Models)
	original.AllowedModels = nil
	require.Same(t, original, original.WithModelRestrictions())
	original.AllowedModels = []string{"gemini-3.8-flash"}
	require.True(t, original.WithModelRestrictions().Group.ModelAllowlistEnabled())
	require.False(t, original.WithModelRestrictions().Group.ModelAllowlist.Allows("gemini-3.8-flash"))
}

func TestAPIKeyAllowedModelsAuthCacheRoundtrip(t *testing.T) {
	svc := &APIKeyService{}
	key := profitAuthTestAPIKey()
	key.AllowedModels = []string{"claude-sonnet-4-6"}
	entry := &APIKeyAuthCacheEntry{Snapshot: svc.snapshotFromAPIKey(context.Background(), key)}
	payload, err := json.Marshal(entry)
	require.NoError(t, err)
	var restored APIKeyAuthCacheEntry
	require.NoError(t, json.Unmarshal(payload, &restored))
	got, used, err := svc.applyAuthCacheEntry("sk-test", &restored)
	require.NoError(t, err)
	require.True(t, used)
	require.Equal(t, key.AllowedModels, got.AllowedModels)
	restored.Snapshot.Version = 24
	_, used, err = svc.applyAuthCacheEntry("sk-test", &restored)
	require.NoError(t, err)
	require.False(t, used)
}
