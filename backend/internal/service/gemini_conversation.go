package service

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/binary"
	"encoding/json"
	"math"
	"strconv"
	"strings"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/tidwall/gjson"
)

// GenerateConversationHash keeps Gemini conversations on one account across
// turns. Other models retain their existing protocol-specific scheduling.
// Explicit conversation IDs are scoped to the API key; without one, the first
// user message is the stable anchor, including image and tool content.
func (s *GatewayService) GenerateConversationHash(parsed *ParsedRequest, model, clientSessionID string) string {
	if !strings.HasPrefix(strings.ToLower(model), "gemini-") {
		return s.GenerateSessionHash(parsed)
	}
	if parsed == nil {
		return ""
	}
	keyID := int64(0)
	if parsed.SessionContext != nil {
		keyID = parsed.SessionContext.APIKeyID
	}
	scope := "gemini-conversation:v1:" + strconv.FormatInt(keyID, 10) + ":"
	if sessionID := GeminiConversationID(parsed, clientSessionID); sessionID != "" {
		return s.hashContent(scope + "id:" + sessionID)
	}

	// Never anchor to a shared system/cache_control prefix alone: unrelated
	// conversations from the same client would all converge on one account.
	anchor := firstGeminiConversationMessage(parsed.MessagesRaw())
	if anchor == "" {
		anchor = firstGeminiConversationMessage(parsed.InputRaw())
	}
	if anchor == "" {
		return ""
	}
	return s.hashContent(scope + "content:" + extractTextFromSystemRaw(parsed.SystemRaw()) + "|" + anchor)
}

// GeminiConversationID also lets native handlers avoid fuzzy content matching
// across distinct explicit client conversations with identical opening prompts.
func GeminiConversationID(parsed *ParsedRequest, clientSessionID string) string {
	if parsed != nil {
		if uid := ParseMetadataUserID(parsed.MetadataUserID); uid != nil && uid.SessionID != "" {
			return uid.SessionID
		}
	}
	if sessionID := sanitizeSessionID(clientSessionID); sessionID != "" {
		return sessionID
	}
	if parsed != nil && parsed.Body != nil {
		for _, path := range []string{"conversation_id", "metadata.conversation_id", "metadata.session_id", "session_id", "prompt_cache_key"} {
			value := gjson.GetBytes(parsed.Body.Bytes(), path)
			if value.Type == gjson.String {
				if sessionID := sanitizeSessionID(value.String()); sessionID != "" {
					return sessionID
				}
			}
		}
	}
	return ""
}

func firstGeminiConversationMessage(raw []byte) string {
	input := parseRawJSONView(raw)
	if input.Type == gjson.String {
		return input.Raw
	}
	if !input.IsArray() {
		return ""
	}
	anchor := ""
	input.ForEach(func(_, item gjson.Result) bool {
		if item.Type == gjson.String || item.Get("role").String() == "user" || item.Get("type").String() == "input_text" {
			if text, ok := geminiConversationTextAnchor(item); ok {
				anchor = strconv.Quote(text)
				return false
			}
			var compact bytes.Buffer
			if json.Compact(&compact, []byte(item.Raw)) == nil {
				anchor = compact.String()
			}
			return false
		}
		return true
	})
	return anchor
}

func geminiConversationTextAnchor(item gjson.Result) (string, bool) {
	if item.Type == gjson.String {
		return item.String(), true
	}
	if item.Get("type").String() == "input_text" {
		return item.Get("text").String(), true
	}
	content := item.Get("content")
	if content.Type == gjson.String {
		return content.String(), true
	}
	if !content.IsArray() {
		content = item.Get("parts")
	}
	if !content.IsArray() {
		return "", false
	}
	var text strings.Builder
	onlyText := true
	content.ForEach(func(_, part gjson.Result) bool {
		kind := part.Get("type").String()
		if part.Get("text").Type != gjson.String || (kind != "" && kind != "text" && kind != "input_text") {
			onlyText = false
			return false
		}
		text.WriteString(part.Get("text").String())
		return true
	})
	return text.String(), onlyText
}

func geminiSchedulingConfig(cfg config.GatewaySchedulingConfig, model string) config.GatewaySchedulingConfig {
	if strings.HasPrefix(strings.ToLower(model), "gemini-") {
		cfg.StickySessionWaitTimeout = min(cfg.StickySessionWaitTimeout, 2*time.Second)
		cfg.FallbackWaitTimeout = min(cfg.FallbackWaitTimeout, 3*time.Second)
	}
	return cfg
}

// Busy Gemini bindings yield to the normal eligible-account selection before
// any queue is entered. Successful sticky acquisitions still take precedence.
func geminiStickyShouldWait(model string, candidateCount int) bool {
	return candidateCount <= 1 || !strings.HasPrefix(strings.ToLower(model), "gemini-")
}

func (s *GatewayService) gatewaySchedulingLoads(ctx context.Context, accounts []AccountWithConcurrency, model string) (map[int64]*AccountLoadInfo, error) {
	if strings.HasPrefix(strings.ToLower(model), "gemini-") {
		// A burst must see slots just acquired by other conversations, rather
		// than reusing the same 200 ms pre-burst load snapshot.
		return s.concurrencyService.GetAccountsLoadBatchFresh(ctx, accounts)
	}
	return s.concurrencyService.GetAccountsLoadBatch(ctx, accounts)
}

// Weighted rendezvous breaks equal-load ties without waiting for asynchronous
// last_used_at writes. Account slot limits and priority/load filtering still
// apply first; a 10-slot account receives less traffic than a 32-slot account.
func geminiConversationAccountScore(sessionHash string, account *Account) float64 {
	digest := sha256.Sum256([]byte(sessionHash + ":" + strconv.FormatInt(account.ID, 10)))
	u := (float64(binary.BigEndian.Uint64(digest[:8])>>11) + 1) / (float64(uint64(1)<<53) + 1)
	return -math.Log(u) / float64(account.EffectiveLoadFactor())
}

func selectGeminiConversationAccount(accounts []accountWithLoad, sessionHash string, preferOAuth bool) *accountWithLoad {
	hasOAuth := false
	if preferOAuth {
		for _, candidate := range accounts {
			hasOAuth = hasOAuth || candidate.account.Type == AccountTypeOAuth
		}
	}
	var selected *accountWithLoad
	for i := range accounts {
		if hasOAuth && accounts[i].account.Type != AccountTypeOAuth {
			continue
		}
		if selected == nil || geminiConversationAccountScore(sessionHash, accounts[i].account) < geminiConversationAccountScore(sessionHash, selected.account) {
			selected = &accounts[i]
		}
	}
	return selected
}
