//go:build unit

package service

import (
	"context"
	"errors"
	"strings"
	"testing"
)

type chatKeyTestRepository struct {
	APIKeyRepository
	key *APIKey
}

func (r *chatKeyTestRepository) GetByID(context.Context, int64) (*APIKey, error) { return r.key, nil }
func (r *chatKeyTestRepository) GetKeyAndOwnerID(context.Context, int64) (string, int64, error) {
	return r.key.Key, r.key.UserID, nil
}

func TestChatKeyCannotBeManagedAsUserKey(t *testing.T) {
	secret := ChatKeyPrefix + strings.Repeat("a", 46)
	repo := &chatKeyTestRepository{key: &APIKey{ID: 1, UserID: 7, Key: secret}}
	svc := &APIKeyService{apiKeyRepo: repo}
	if err := svc.ValidateCustomKey(secret); !errors.Is(err, ErrAPIKeyInvalidChars) {
		t.Fatalf("reserved key accepted: %v", err)
	}
	if _, err := svc.Update(context.Background(), 1, 7, UpdateAPIKeyRequest{}); !errors.Is(err, ErrAPIKeyNotFound) {
		t.Fatalf("internal key update accepted: %v", err)
	}
	if err := svc.Delete(context.Background(), 1, 7); !errors.Is(err, ErrAPIKeyNotFound) {
		t.Fatalf("internal key delete accepted: %v", err)
	}
	if PublicAPIKey(secret) != "" {
		t.Fatal("internal credential leaked")
	}
	if PublicAPIKey("sk-normal") != "sk-normal" {
		t.Fatal("normal key unexpectedly redacted")
	}
}
