//go:build unit

package service

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
)

func glmImageCacheContext(keyID int64) *gin.Context {
	c, _ := gin.CreateTestContext(httptest.NewRecorder())
	groupID := int64(9)
	c.Set("api_key", &APIKey{ID: keyID, UserID: 20, GroupID: &groupID})
	return c
}

func TestGLMImageDescriptionCacheScopesAndMutableURLs(t *testing.T) {
	account, _, _, _ := glmVisionFixture()
	c := glmImageCacheContext(10)
	uri := "data:image/png;base64,AAAA"
	key := glmVisionDescriptionCacheKey(c, account, uri)
	require.NotEmpty(t, key)
	require.NotContains(t, key, "AAAA")
	require.NotEqual(t, key, glmVisionDescriptionCacheKey(glmImageCacheContext(11), account, uri))
	apiKey, _ := c.Get("api_key")
	apiKey.(*APIKey).UserID++
	require.NotEqual(t, key, glmVisionDescriptionCacheKey(c, account, uri))
	apiKey.(*APIKey).UserID--
	*apiKey.(*APIKey).GroupID++
	require.NotEqual(t, key, glmVisionDescriptionCacheKey(c, account, uri))
	*apiKey.(*APIKey).GroupID--
	account.Extra["glm_image_bridge_fallback_model"] = "claude-sonnet-4-6"
	require.NotEqual(t, key, glmVisionDescriptionCacheKey(c, account, uri))
	account.Extra["glm_image_bridge_fallback_model"] = ""
	before := glmVisionDescriptionCacheKey(c, account, uri)
	account.Extra["glm_image_bridge_reasoning_effort"] = "low"
	require.NotEqual(t, before, glmVisionDescriptionCacheKey(c, account, uri), "a different helper effort must not reuse an older description")
	require.Empty(t, glmVisionDescriptionCacheKey(c, account, "https://example.com/mutable.png"))
	require.Empty(t, glmVisionDescriptionCacheKey(nil, account, uri))
}

func TestGLMImageDescriptionCacheExpiryAndMemoryBounds(t *testing.T) {
	cache := glmVisionDescriptionCache{maxEntries: 2, maxBytes: 12, ttl: time.Minute}
	now := time.Now()
	cache.putLocked("a", "1111", now)
	cache.putLocked("b", "2222", now)
	_, ok := cache.getLocked("a", now.Add(30*time.Second))
	require.True(t, ok)
	cache.putLocked("c", "3333", now)
	_, ok = cache.getLocked("b", now)
	require.False(t, ok, "least recently used entry should be evicted")
	require.LessOrEqual(t, cache.bytes, 12)
	_, ok = cache.getLocked("a", now.Add(time.Minute))
	require.False(t, ok, "cache hits must not extend expiry")
	cache.putLocked("oversized", strings.Repeat("x", 12), now)
	require.NotContains(t, cache.entries, "oversized")
	cache.putLocked("d", "44444444", now)
	require.Len(t, cache.entries, 1, "byte bound should also evict entries")
	require.LessOrEqual(t, cache.bytes, 12)
	cache.putLocked("e", "5555", now.Add(2*time.Minute))
	require.NotContains(t, cache.entries, "d", "insertion should purge expired descriptions")
}

func TestGLMImageDescriptionCacheConcurrentReuseAndBilling(t *testing.T) {
	var cache glmVisionDescriptionCache
	var calls, billed atomic.Int32
	started, release := make(chan struct{}), make(chan struct{})
	load := func(context.Context) (string, glmVisionUsage, error) {
		calls.Add(1)
		close(started)
		<-release
		return "4729", glmVisionUsage{Account: &Account{ID: 1}}, nil
	}
	var wg sync.WaitGroup
	errors := make(chan error, 10)
	for index := 0; index < 10; index++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			description, usage, err := cache.describe(context.Background(), "same", load)
			if err != nil || description != "4729" {
				errors <- fmt.Errorf("unexpected result: %q, %v", description, err)
			}
			if usage != nil {
				billed.Add(1)
			}
		}()
	}
	<-started
	close(release)
	wg.Wait()
	close(errors)
	for err := range errors {
		require.NoError(t, err)
	}
	require.Equal(t, int32(1), calls.Load())
	require.Equal(t, int32(1), billed.Load(), "only the helper call owner should record usage")
	_, usage, err := cache.describe(context.Background(), "same", load)
	require.NoError(t, err)
	require.Nil(t, usage, "later history reuse should not incur helper charges")
}

func TestGLMImageDescriptionCacheFailureAndCancellation(t *testing.T) {
	var cache glmVisionDescriptionCache
	load := func(context.Context) (string, glmVisionUsage, error) {
		return "", glmVisionUsage{}, errors.New("unavailable")
	}
	_, _, err := cache.describe(context.Background(), "failed", load)
	require.Error(t, err)
	require.Empty(t, cache.entries)
	require.Empty(t, cache.flights)

	ctx, cancel := context.WithCancel(context.Background())
	started := make(chan struct{})
	owner := make(chan error, 1)
	go func() {
		_, _, err := cache.describe(ctx, "canceled", func(ctx context.Context) (string, glmVisionUsage, error) {
			close(started)
			<-ctx.Done()
			return "", glmVisionUsage{}, ctx.Err()
		})
		owner <- err
	}()
	<-started
	follower := make(chan error, 1)
	go func() {
		_, usage, err := cache.describe(context.Background(), "canceled", func(context.Context) (string, glmVisionUsage, error) {
			return "retry succeeded", glmVisionUsage{Account: &Account{ID: 1}}, nil
		})
		if err == nil && usage == nil {
			err = errors.New("retry owner should receive usage")
		}
		follower <- err
	}()
	cancel()
	require.ErrorIs(t, <-owner, context.Canceled)
	require.NoError(t, <-follower)
}

type glmImageParallelForwarder struct {
	started chan struct{}
	release <-chan struct{}
	calls   atomic.Int32
}

func (f *glmImageParallelForwarder) ForwardAsChatCompletions(ctx context.Context, c *gin.Context,
	account *Account, body []byte, _ *ParsedRequest,
) (*ForwardResult, error) {
	// Native OAuth refresh can fill a project ID on the forwarded account.
	account.Credentials["project_id"] = "image-local-project"
	f.calls.Add(1)
	f.started <- struct{}{}
	select {
	case <-ctx.Done():
		return nil, ctx.Err()
	case <-f.release:
	}
	uri := gjson.GetBytes(body, "messages.0.content.1.image_url.url").String()
	description := "image " + glmImageDigest(uri)
	encoded, _ := json.Marshal(description)
	c.Data(http.StatusOK, "application/json", []byte(`{"choices":[{"message":{"content":`+string(encoded)+`},"finish_reason":"stop"}]}`))
	return &ForwardResult{Model: "gemini-3.8-flash", Usage: ClaudeUsage{InputTokens: 150, OutputTokens: 20}}, nil
}

func TestGLMImageBridgeParallelImagesAndHistoryReuse(t *testing.T) {
	account, vision, svc, _ := glmVisionFixture()
	vision.ID, vision.Platform, vision.Type = 1, PlatformAntigravity, AccountTypeOAuth
	vision.Credentials["model_mapping"] = map[string]any{"gemini-3.8-flash": "gemini-3.8-flash-low"}
	account.Extra["glm_image_bridge_account_id"] = 1
	account.Extra["glm_image_bridge_model"] = "gemini-3.8-flash"
	release := make(chan struct{})
	var releaseOnce sync.Once
	defer releaseOnce.Do(func() { close(release) })
	forwarder := &glmImageParallelForwarder{started: make(chan struct{}, 16), release: release}
	svc.ConfigureGLMClaudeVision(forwarder, &GatewayService{})
	uri := func(suffix string) string {
		return "data:image/png;base64," + base64.StdEncoding.EncodeToString([]byte("\x89PNG\r\n\x1a\n"+suffix))
	}
	first, second := uri("first"), uri("second")
	body := []byte(`{"model":"glm-5.3","thinking":{"type":"enabled"},"reasoning_effort":"xhigh","messages":[{"role":"user","content":[{"type":"image_url","image_url":{"url":"` + first + `"}},{"type":"image_url","image_url":{"url":"` + second + `"}},{"type":"image_url","image_url":{"url":"` + first + `"}}]}]}`)
	c := glmImageCacheContext(10)
	type result struct {
		body []byte
		err  error
	}
	finished := make(chan result, 1)
	go func() {
		body, err := svc.bridgeGLMImageInputs(context.Background(), c, account, body)
		finished <- result{body, err}
	}()
	for index := 0; index < 2; index++ {
		select {
		case <-forwarder.started:
		case <-time.After(time.Second):
			t.Fatal("distinct images must both start before either helper finishes")
		}
	}
	releaseOnce.Do(func() { close(release) })
	got := <-finished
	require.NoError(t, got.err)
	require.Equal(t, int32(2), forwarder.calls.Load(), "duplicates must be deduplicated before parallel work")
	require.Equal(t, "xhigh", gjson.GetBytes(got.body, "reasoning_effort").String())
	require.Contains(t, gjson.GetBytes(got.body, "messages.0.content.0.text").String(), glmImageDigest(first))
	require.Contains(t, gjson.GetBytes(got.body, "messages.0.content.1.text").String(), glmImageDigest(second))
	require.Len(t, glmVisionRequestState(c).Usage, 2)
	require.NotContains(t, vision.Credentials, "project_id")

	history := glmImageCacheContext(10)
	reused, err := svc.bridgeGLMImageInputs(context.Background(), history, account, body)
	require.NoError(t, err)
	require.JSONEq(t, string(got.body), string(reused))
	require.Equal(t, int32(2), forwarder.calls.Load())
	require.Empty(t, glmVisionRequestState(history).Usage)

	_, err = svc.bridgeGLMImageInputs(context.Background(), glmImageCacheContext(11), account, body)
	require.NoError(t, err)
	require.Equal(t, int32(4), forwarder.calls.Load(), "another API key must analyze its own images")
}
