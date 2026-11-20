package handler

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"net/netip"
	"net/url"
	"regexp"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"

	middleware2 "github.com/Wei-Shaw/sub2api/internal/server/middleware"
	"github.com/Wei-Shaw/sub2api/internal/service"
	"github.com/gin-gonic/gin"
	"github.com/google/uuid"
	"github.com/tidwall/gjson"
)

type voiceSearchLeaseKey struct{}

type voiceSearchLease struct {
	userID, keyID, groupID, accountID int64
}

func (lease voiceSearchLease) matches(userID int64, key *service.APIKey) bool {
	return lease.accountID > 0 && key != nil && key.GroupID != nil && lease.userID == userID &&
		key.UserID == userID && lease.keyID == key.ID && lease.groupID == *key.GroupID
}

func (lease voiceSearchLease) borrowAccountSlot(selection *service.AccountSelectionResult) {
	// The scheduler must independently select this account for alpha search.
	// Preserve its eligibility/profit gate and never release the parent slot.
	if !selection.Acquired && selection.WaitPlan != nil && selection.ReleaseFunc == nil && selection.Account.ID == lease.accountID {
		selection.Acquired = true
	}
}

const browserVoiceSearchDispatch = "browser_voice_search_dispatch"

// BindBrowserRealtimeSearch captures the site's login before chatCredential
// replaces it with the private gateway key. The nested request traverses the
// same authentication, group checks, model routing and billing as website search.
func BindBrowserRealtimeSearch(c *gin.Context, router http.Handler, login string) {
	c.Set(browserVoiceSearchDispatch, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		r.Header.Set("Authorization", login)
		router.ServeHTTP(w, r)
	}))
}

func browserVoiceSearchModel(key *service.APIKey) string {
	if !realtimeAPIKeyGroupEnabled(key) {
		return ""
	}
	for _, model := range service.ToolSearchModels {
		if key.Group.ModelAllowlist.Allows(model) {
			return model
		}
	}
	if key.Group.ModelAllowlist.Allows(service.ToolWebSearch) {
		return service.ToolSearchModels[0]
	}
	return ""
}

func (h *OpenAIGatewayHandler) browserRealtimeSearch(ctx context.Context, origin *gin.Context, heldAccountID int64, query string) (json.RawMessage, error) {
	query = strings.TrimSpace(query)
	if query == "" || utf8.RuneCountInString(query) > 2000 {
		return nil, errors.New("搜索词应为 1 到 2000 个字符")
	}
	key, _ := middleware2.GetAPIKeyFromContext(origin)
	subject, ok := middleware2.GetAuthSubjectFromContext(origin)
	binding, _ := origin.Get(browserVoiceSearchDispatch)
	dispatch, bound := binding.(http.HandlerFunc)
	model := browserVoiceSearchModel(key)
	if !origin.GetBool("internal_chat_authenticated") || !bound || !ok || heldAccountID <= 0 || model == "" || key.GroupID == nil || subject.UserID != key.UserID {
		return nil, errors.New("当前语音会话未开通网页搜索")
	}
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	// Use a fresh request identity and pricing context for each separately
	// billed search while following disconnection/cancellation of this call.
	searchCtx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	stop := context.AfterFunc(ctx, cancel)
	defer stop()
	lease := voiceSearchLease{subject.UserID, key.ID, *key.GroupID, heldAccountID}
	searchCtx = context.WithValue(searchCtx, voiceSearchLeaseKey{}, lease)
	body, err := json.Marshal(gin.H{"model": model, "id": "search-" + uuid.NewString(),
		"commands": gin.H{"search_query": []gin.H{{"q": query}}, "response_length": "short"},
		"settings": gin.H{"external_web_access": true, "search_context_size": "medium"}})
	if err != nil {
		return nil, err
	}
	req, err := http.NewRequestWithContext(searchCtx, http.MethodPost, "/api/v1/chat/alpha/search", bytes.NewReader(body))
	if err != nil {
		return nil, err
	}
	req.RequestURI = req.URL.RequestURI()
	req.RemoteAddr = origin.Request.RemoteAddr
	req.Host = origin.Request.Host
	req.Header = origin.Request.Header.Clone()
	for _, name := range []string{"Authorization", "X-Api-Key", "X-Goog-Api-Key", "Cookie", "Connection", "Upgrade", "Sec-WebSocket-Key", "Sec-WebSocket-Version", "Sec-WebSocket-Protocol", "Sec-WebSocket-Extensions", "X-Request-Id", "X-Client-Request-Id", "Idempotency-Key", "Accept-Encoding", "Content-Length"} {
		req.Header.Del(name)
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "application/json")
	rec := httptest.NewRecorder()
	dispatch(rec, req)
	if err := searchCtx.Err(); err != nil {
		return nil, err
	}
	if rec.Code != http.StatusOK {
		return nil, fmt.Errorf("网页搜索暂不可用（HTTP %d）", rec.Code)
	}
	return compactVoiceSearch(rec.Body.Bytes())
}

type voiceSearchSource struct {
	URL     string `json:"url"`
	Title   string `json:"title"`
	Snippet string `json:"snippet,omitempty"`
}

var voiceSearchCitation = regexp.MustCompile("\\x{e200}[^\\x{e201}]*\\x{e201}")
var voiceSearchHeading = regexp.MustCompile(`(?m)^([^\n]{1,500}) \((https?://[^[:space:]]+)\)\r?$`)

func shortVoiceSearchText(s string, limit int) string {
	s = strings.Join(strings.Fields(voiceSearchCitation.ReplaceAllString(s, "")), " ")
	for i := range s {
		if limit == 0 {
			return s[:i]
		}
		limit--
	}
	return s
}

func voiceSourceURL(raw string) bool {
	u, err := url.Parse(raw)
	if err != nil || len(raw) > 2000 || u.User != nil || (u.Scheme != "http" && u.Scheme != "https") || strings.ContainsAny(raw, "\\\r\n\t ") {
		return false
	}
	host := strings.ToLower(strings.TrimSuffix(u.Hostname(), "."))
	if port := u.Port(); port != "" {
		n, err := strconv.ParseUint(port, 10, 16)
		if err != nil || n == 0 {
			return false
		}
	}
	if ip, err := netip.ParseAddr(host); err == nil {
		ip = ip.Unmap()
		return ip.IsGlobalUnicast() && !ip.IsPrivate() && ip.Zone() == ""
	}
	// Browsers can interpret a numeric final label as a non-canonical IPv4 address.
	last := host[strings.LastIndexByte(host, '.')+1:]
	_, numericErr := strconv.ParseUint(last, 10, 64)
	return strings.Contains(host, ".") && last != "" && numericErr != nil && !strings.HasPrefix(last, "0x") &&
		!strings.HasSuffix(host, ".local") && !strings.HasSuffix(host, ".internal") && !strings.HasSuffix(host, ".localhost")
}

func compactVoiceSearch(raw []byte) (json.RawMessage, error) {
	if !gjson.ValidBytes(raw) {
		return nil, errors.New("网页搜索返回了无效的结果")
	}
	value := gjson.ParseBytes(raw)
	output, results := value.Get("output"), value.Get("results")
	if output.Type != gjson.String && !results.IsArray() && !results.IsObject() {
		return nil, errors.New("网页搜索未返回结果")
	}
	sources := make([]voiceSearchSource, 0, 4)
	seen := make(map[string]bool)
	add := func(link, title, snippet string) {
		if len(sources) == cap(sources) || seen[link] || !voiceSourceURL(link) {
			return
		}
		seen[link] = true
		sources = append(sources, voiceSearchSource{link, shortVoiceSearchText(title, 140), shortVoiceSearchText(snippet, 300)})
	}
	results.ForEach(func(_, result gjson.Result) bool {
		snippet := result.Get("snippet").String()
		if snippet == "" {
			snippet = result.Get("content").String()
		}
		if snippet == "" {
			snippet = result.Get("text").String()
		}
		add(result.Get("url").String(), result.Get("title").String(), snippet)
		return len(sources) < cap(sources)
	})
	text := output.String()
	if len(sources) < cap(sources) {
		for _, match := range voiceSearchHeading.FindAllStringSubmatch(text, 12) {
			add(match[2], match[1], "")
		}
	}
	summary := ""
	if len(sources) > 0 {
		summary = shortVoiceSearchText(text, 1600)
	}
	payload := struct {
		Sources []voiceSearchSource `json:"sources"`
		Summary string              `json:"summary,omitempty"`
	}{sources, summary}
	encoded, err := json.Marshal(payload)
	if len(encoded) > 16*1024 {
		payload.Summary = ""
		encoded, err = json.Marshal(payload)
		for len(encoded) > 16*1024 {
			payload.Sources = payload.Sources[:len(payload.Sources)-1]
			encoded, err = json.Marshal(payload)
		}
	}
	return encoded, err
}
