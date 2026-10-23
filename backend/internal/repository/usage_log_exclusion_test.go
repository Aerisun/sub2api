//go:build unit

package repository

import (
	"context"
	"testing"
	"time"

	"github.com/DATA-DOG/go-sqlmock"
	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/Wei-Shaw/sub2api/internal/service"
	"github.com/stretchr/testify/require"
)

func TestUsageLogRepositoryExcludedAPIKey(t *testing.T) {
	db, mock, err := sqlmock.New()
	require.NoError(t, err)
	t.Cleanup(func() { _ = db.Close() })
	cfg := &config.Config{}
	cfg.Gateway.UsageRecord.ExcludedAPIKeyIDs = []int64{16}
	repo := NewUsageLogRepository(nil, db, cfg).(*usageLogRepository)
	log := &service.UsageLog{APIKeyID: 16, RequestID: "monitor-probe"}

	// Exclusions take effect before either writer starts a batcher or touches SQL.
	inserted, err := repo.Create(context.Background(), log)
	require.NoError(t, err)
	require.False(t, inserted)
	require.NoError(t, repo.CreateBestEffort(context.Background(), log))
	require.Nil(t, repo.createBatchCh)
	require.Nil(t, repo.bestEffortBatchCh)
	require.Zero(t, log.ID)
	require.NoError(t, mock.ExpectationsWereMet())

	// A neighboring customer key still reaches the normal insert path.
	customerLog := &service.UsageLog{APIKeyID: 17, Model: "test-model", CreatedAt: time.Now()}
	mock.ExpectQuery("INSERT INTO usage_logs").
		WillReturnRows(sqlmock.NewRows([]string{"id", "created_at"}).AddRow(int64(101), customerLog.CreatedAt))
	inserted, err = repo.Create(context.Background(), customerLog)
	require.NoError(t, err)
	require.True(t, inserted)
	require.Equal(t, int64(101), customerLog.ID)
	require.NoError(t, mock.ExpectationsWereMet())
}

func TestUsageLogRepositoryExclusionConfigIsCopied(t *testing.T) {
	cfg := &config.Config{}
	cfg.Gateway.UsageRecord.ExcludedAPIKeyIDs = []int64{16}
	repo := NewUsageLogRepository(nil, nil, cfg).(*usageLogRepository)
	cfg.Gateway.UsageRecord.ExcludedAPIKeyIDs[0] = 17
	require.True(t, repo.excludesAPIKey(16))
	require.False(t, repo.excludesAPIKey(17))
	require.False(t, NewUsageLogRepository(nil, nil, nil).(*usageLogRepository).excludesAPIKey(16))
}
