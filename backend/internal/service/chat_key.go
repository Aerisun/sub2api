package service

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"strings"
)

// Reserved credential namespace: never accepted from clients or serialized to them.
const ChatKeyPrefix = "sk-internal-chat-"

func IsChatKey(key string) bool { return strings.HasPrefix(key, ChatKeyPrefix) }
func PublicAPIKey(key string) string {
	if IsChatKey(key) {
		return ""
	}
	return key
}

type chatKeyRepository interface {
	EnsureChatKey(context.Context, int64, int64, string) (string, error)
}

// ChatCredential lazily provisions one balance-billed key per user. The database
// unique index serializes first use across requests and server processes.
func (s *APIKeyService) ChatCredential(ctx context.Context, userID int64) (string, error) {
	groups, err := s.GetAvailableGroups(ctx, userID)
	if err != nil {
		return "", err
	}
	var selected *Group
	for i := range groups {
		g := &groups[i]
		if g.IsSubscriptionType() {
			continue
		}
		if selected == nil || (g.Platform == PlatformComposite && selected.Platform != PlatformComposite) {
			selected = g
		}
	}
	if selected == nil {
		return "", errors.New("no balance-billed chat group is available")
	}
	repo, ok := s.apiKeyRepo.(chatKeyRepository)
	if !ok {
		return "", errors.New("chat key storage unavailable")
	}
	var secret [23]byte
	if _, err = rand.Read(secret[:]); err != nil {
		return "", err
	}
	credential, err := repo.EnsureChatKey(ctx, userID, selected.ID, ChatKeyPrefix+hex.EncodeToString(secret[:]))
	if err != nil {
		return "", err
	}
	// A newly available composite group can rebind the key. Do not reuse a
	// cached authentication snapshot of its previous group or permissions.
	s.InvalidateAuthCacheByKey(ctx, credential)
	return credential, nil
}
