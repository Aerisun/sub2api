package handler

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/server/middleware"
	"github.com/Wei-Shaw/sub2api/internal/service"
	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
)

func voiceSearchTestOrigin(t *testing.T, dispatch http.HandlerFunc) *gin.Context {
	t.Helper()
	c, _ := gin.CreateTestContext(httptest.NewRecorder())
	c.Request = httptest.NewRequest(http.MethodGet, "/v1/realtime", nil)
	c.Request.Header.Set("Authorization", "Bearer internal-test-key")
	c.Request.Header.Set("Sec-WebSocket-Protocol", "test-protocol")
	c.Request.Header.Set("X-Request-Id", "voice-request-id")
	c.Request.Header.Set("X-Real-IP", "203.0.113.77")
	groupID := int64(103)
	key := &service.APIKey{ID: 11, UserID: 22, GroupID: &groupID, Group: &service.Group{
		ID: groupID, Platform: service.PlatformComposite, ModelAllowlist: service.GroupModelAllowlist{
			Enabled: true, Models: []string{"gpt-realtime-2.1-mini", "gpt-6.1-sol"},
		},
	}}
	c.Set(string(middleware.ContextKeyAPIKey), key)
	c.Set(string(middleware.ContextKeyUser), middleware.AuthSubject{UserID: key.UserID, Concurrency: 1})
	c.Set("internal_chat_authenticated", true)
	BindBrowserRealtimeSearch(c, dispatch, "Bearer logged-in-test-user")
	return c
}

func TestBrowserRealtimeSearchDispatch(t *testing.T) {
	ids := map[string]bool{}
	c := voiceSearchTestOrigin(t, func(w http.ResponseWriter, r *http.Request) {
		require.Equal(t, http.MethodPost, r.Method)
		require.Equal(t, "/api/v1/chat/alpha/search", r.RequestURI)
		require.Equal(t, "Bearer logged-in-test-user", r.Header.Get("Authorization"))
		require.Empty(t, r.Header.Get("Sec-WebSocket-Protocol"))
		require.Empty(t, r.Header.Get("X-Request-Id"))
		require.Equal(t, "203.0.113.77", r.Header.Get("X-Real-IP"))
		require.Equal(t, voiceSearchLease{22, 11, 103, 91}, r.Context().Value(voiceSearchLeaseKey{}))
		body, err := io.ReadAll(r.Body)
		require.NoError(t, err)
		require.Equal(t, "gpt-6.1-sol", gjson.GetBytes(body, "model").String())
		require.Equal(t, "通话内搜索", gjson.GetBytes(body, "commands.search_query.0.q").String())
		id := gjson.GetBytes(body, "id").String()
		require.NotEmpty(t, id)
		require.False(t, ids[id])
		ids[id] = true
		_, _ = io.WriteString(w, `{"results":{"ref":{"url":"https://example.com","title":"Example","text":"Voice result"}}}`)
	})
	h := &OpenAIGatewayHandler{}
	for range 2 {
		out, err := h.browserRealtimeSearch(context.Background(), c, 91, " 通话内搜索 ")
		require.NoError(t, err)
		require.Equal(t, "Voice result", gjson.GetBytes(out, "sources.0.snippet").String())
	}
	require.Len(t, ids, 2)
}

func TestBrowserRealtimeSearchPermissionIdentityAndBorrow(t *testing.T) {
	c := voiceSearchTestOrigin(t, func(http.ResponseWriter, *http.Request) { t.Fatal("search should be rejected") })
	key, _ := middleware.GetAPIKeyFromContext(c)
	lease := voiceSearchLease{22, 11, 103, 91}
	require.True(t, lease.matches(22, key))
	require.False(t, lease.matches(23, key))
	copyKey := *key
	copyKey.ID++
	require.False(t, lease.matches(22, &copyKey))
	otherGroup := int64(89)
	copyKey = *key
	copyKey.GroupID = &otherGroup
	require.False(t, lease.matches(22, &copyKey))

	key.Group.ModelAllowlist.Models = []string{"gpt-realtime-2.1-mini"}
	_, err := (&OpenAIGatewayHandler{}).browserRealtimeSearch(context.Background(), c, 91, "Example")
	require.ErrorContains(t, err, "未开通")
	key.Group.ModelAllowlist.Models = append(key.Group.ModelAllowlist.Models, service.ToolWebSearch)
	require.Equal(t, service.ToolSearchModels[0], browserVoiceSearchModel(key))
	c.Set("internal_chat_authenticated", false)
	_, err = (&OpenAIGatewayHandler{}).browserRealtimeSearch(context.Background(), c, 91, "Example")
	require.ErrorContains(t, err, "未开通")

	selection := &service.AccountSelectionResult{Account: &service.Account{ID: 91}, WaitPlan: &service.AccountWaitPlan{AccountID: 91, MaxConcurrency: 1}}
	lease.borrowAccountSlot(selection)
	require.True(t, selection.Acquired)
	require.Nil(t, selection.ReleaseFunc)
	selection.Acquired = false
	selection.Account.ID = 92
	lease.borrowAccountSlot(selection)
	require.False(t, selection.Acquired)
	selection.Account.ID = 91
	selection.WaitPlan = nil
	lease.borrowAccountSlot(selection)
	require.False(t, selection.Acquired)
}

func TestBrowserRealtimeSearchErrorsAndCancel(t *testing.T) {
	h := &OpenAIGatewayHandler{}
	c := voiceSearchTestOrigin(t, func(w http.ResponseWriter, r *http.Request) {
		http.Error(w, "upstream-private-detail", http.StatusForbidden)
	})
	_, err := h.browserRealtimeSearch(context.Background(), c, 91, "Example")
	require.ErrorContains(t, err, "403")
	require.NotContains(t, err.Error(), "private-detail")
	ctx, cancel := context.WithCancel(context.Background())
	c = voiceSearchTestOrigin(t, func(w http.ResponseWriter, r *http.Request) {
		cancel()
		<-r.Context().Done()
	})
	_, err = h.browserRealtimeSearch(ctx, c, 91, "Example")
	require.True(t, errors.Is(err, context.Canceled))
}

func TestBrowserRealtimeSearchCompactResult(t *testing.T) {
	_, err := compactVoiceSearch([]byte(`{"invalid":true}`))
	require.Error(t, err)
	var unsafe []map[string]string
	for _, link := range []string{"https://127.0.0.1", "https://127.1", "https://name.internal", "https://user:pass@example.com", "https://example.com:99999", "file:///test"} {
		unsafe = append(unsafe, map[string]string{"url": link})
	}
	b, err := json.Marshal(map[string]any{"results": unsafe, "output": "no external source found"})
	require.NoError(t, err)
	out, err := compactVoiceSearch(b)
	require.NoError(t, err)
	require.JSONEq(t, `{"sources":[]}`, string(out))

	var results []map[string]string
	for i := range 4 {
		results = append(results, map[string]string{"title": strings.Repeat("中", 140), "snippet": strings.Repeat("&", 300), "url": "https://example.com/" + string(rune('a'+i)) + strings.Repeat("&", 800)})
	}
	b, err = json.Marshal(map[string]any{"results": results, "output": strings.Repeat("中", 1800)})
	require.NoError(t, err)
	out, err = compactVoiceSearch(b)
	require.NoError(t, err)
	require.LessOrEqual(t, len(out), 16*1024)
	require.True(t, json.Valid(out))
	require.NotEmpty(t, gjson.GetBytes(out, "sources").Array())

	out, err = compactVoiceSearch([]byte(`{"output":"Example (https://example.com/a)\ncitefake\nA useful fact"}`))
	require.NoError(t, err)
	require.Equal(t, "https://example.com/a", gjson.GetBytes(out, "sources.0.url").String())
	require.NotContains(t, gjson.GetBytes(out, "summary").String(), "fake")
}
