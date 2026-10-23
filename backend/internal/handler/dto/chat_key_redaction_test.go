//go:build unit

package dto

import (
	"encoding/json"
	"github.com/Wei-Shaw/sub2api/internal/service"
	"strings"
	"testing"
)

func TestChatCredentialIsRedactedFromUsageAndUserDTOs(t *testing.T) {
	secret := service.ChatKeyPrefix + strings.Repeat("b", 46)
	key := &service.APIKey{ID: 43, Name: "在线对话", UserID: 7, Key: secret}
	for _, out := range []any{APIKeyFromService(key), UsageLogFromService(&service.UsageLog{APIKey: key})} {
		raw, err := json.Marshal(out)
		if err != nil {
			t.Fatal(err)
		}
		if strings.Contains(string(raw), secret) || strings.Contains(string(raw), service.ChatKeyPrefix) {
			t.Fatalf("credential leaked: %s", raw)
		}
	}
}
