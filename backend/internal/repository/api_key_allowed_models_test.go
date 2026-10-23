package repository

import (
	"context"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/service"
	"github.com/stretchr/testify/require"
)

func TestAPIKeyAllowedModelsPersistenceAndAuthProjection(t *testing.T) {
	repo, client := newAPIKeyRepoSQLite(t)
	ctx := context.Background()
	user := mustCreateAPIKeyRepoUser(t, ctx, client, "model-access@test.local")
	key := &service.APIKey{UserID: user.ID, Key: "sk-model-access-test", Name: "Restricted", Status: service.StatusActive, AllowedModels: []string{"claude-sonnet-4-6"}}
	require.NoError(t, repo.Create(ctx, key))
	got, err := repo.GetByKeyForAuth(ctx, key.Key)
	require.NoError(t, err)
	require.Equal(t, key.AllowedModels, got.AllowedModels)
	key.Name = "Renamed"
	require.NoError(t, repo.Update(ctx, key, service.APIKeyUpdateFields{Name: true}))
	got, err = repo.GetByID(ctx, key.ID)
	require.NoError(t, err)
	require.Equal(t, key.AllowedModels, got.AllowedModels)
	key.AllowedModels = []string{"gemini-3.8-flash"}
	require.NoError(t, repo.Update(ctx, key, service.APIKeyUpdateFields{AllowedModels: true}))
	got, err = repo.GetByKeyForAuth(ctx, key.Key)
	require.NoError(t, err)
	require.Equal(t, key.AllowedModels, got.AllowedModels)
	key.AllowedModels = nil
	require.NoError(t, repo.Update(ctx, key, service.APIKeyUpdateFields{AllowedModels: true}))
	got, err = repo.GetByKeyForAuth(ctx, key.Key)
	require.NoError(t, err)
	require.Empty(t, got.AllowedModels)
}
