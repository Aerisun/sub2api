package service

import (
	"container/list"
	"context"
	"fmt"
	"strings"
	"sync"
	"time"

	"github.com/gin-gonic/gin"
)

const (
	auxiliaryVisionDescriptionTTL        = 10 * time.Minute
	auxiliaryVisionDescriptionMaxEntries = 1024
	auxiliaryVisionDescriptionMaxBytes   = 16 * 1024 * 1024
	auxiliaryVisionImageParallelism      = 2
)

type auxiliaryVisionDescriptionEntry struct {
	key, description string
	expires          time.Time
	bytes            int
}

type auxiliaryVisionDescriptionFlight struct {
	done        chan struct{}
	description string
	err         error
	retry       bool
}

// This bounded, process-local cache stores descriptions only. Customer identity
// and helper configuration scope every key; mutable remote URLs are never cached.
// A flight's owner records usage, while followers reuse its description for free.
type auxiliaryVisionDescriptionCache struct {
	mu      sync.Mutex
	entries map[string]*list.Element
	lru     list.List
	flights map[string]*auxiliaryVisionDescriptionFlight
	bytes   int

	// Zero values use the production limits; tests can use smaller bounds.
	ttl        time.Duration
	maxEntries int
	maxBytes   int
}

func auxiliaryVisionDescriptionCacheKey(c *gin.Context, account *Account, imageURL string) string {
	if c == nil || account == nil || !strings.HasPrefix(strings.ToLower(imageURL), "data:image/") {
		return ""
	}
	value, exists := c.Get("api_key")
	key, ok := value.(*APIKey)
	if !exists || !ok || key == nil || key.ID <= 0 || key.UserID <= 0 {
		return ""
	}
	var groupID int64
	if key.GroupID != nil {
		groupID = *key.GroupID
	}
	return fmt.Sprintf("v2:%d:%d:%d:%d:%q:%d:%q:%q:%s", key.UserID, key.ID, groupID,
		account.getExtraInt("image_bridge_account_id"), account.getExtraString("image_bridge_model"),
		account.getExtraInt("image_bridge_fallback_account_id"), account.getExtraString("image_bridge_fallback_model"),
		auxiliaryVisionReasoningEffort(account), auxiliaryImageDigest(imageURL))
}

func (cache *auxiliaryVisionDescriptionCache) removeLocked(element *list.Element) {
	entry, _ := element.Value.(*auxiliaryVisionDescriptionEntry)
	delete(cache.entries, entry.key)
	cache.bytes -= entry.bytes
	cache.lru.Remove(element)
}

func (cache *auxiliaryVisionDescriptionCache) getLocked(key string, now time.Time) (string, bool) {
	element := cache.entries[key]
	if element == nil {
		return "", false
	}
	entry, _ := element.Value.(*auxiliaryVisionDescriptionEntry)
	if !now.Before(entry.expires) {
		cache.removeLocked(element)
		return "", false
	}
	cache.lru.MoveToFront(element)
	return entry.description, true
}

func (cache *auxiliaryVisionDescriptionCache) putLocked(key, description string, now time.Time) {
	ttl, maxEntries, maxBytes := cache.ttl, cache.maxEntries, cache.maxBytes
	if ttl <= 0 {
		ttl = auxiliaryVisionDescriptionTTL
	}
	if maxEntries <= 0 {
		maxEntries = auxiliaryVisionDescriptionMaxEntries
	}
	if maxBytes <= 0 {
		maxBytes = auxiliaryVisionDescriptionMaxBytes
	}
	size := len(key) + len(description)
	if strings.TrimSpace(description) == "" || size > maxBytes {
		return
	}
	if cache.entries == nil {
		cache.entries = make(map[string]*list.Element)
	}
	if existing := cache.entries[key]; existing != nil {
		cache.removeLocked(existing)
	}
	// Hits do not extend expiry. Expired entries are removed on insertion as well
	// as lookup, so old conversations cannot occupy the bounded cache forever.
	for element := cache.lru.Back(); element != nil; {
		previous := element.Prev()
		entry, _ := element.Value.(*auxiliaryVisionDescriptionEntry)
		if !now.Before(entry.expires) {
			cache.removeLocked(element)
		}
		element = previous
	}
	for len(cache.entries) >= maxEntries || cache.bytes+size > maxBytes {
		cache.removeLocked(cache.lru.Back())
	}
	entry := &auxiliaryVisionDescriptionEntry{key: key, description: description, expires: now.Add(ttl), bytes: size}
	cache.entries[key] = cache.lru.PushFront(entry)
	cache.bytes += size
}

func (cache *auxiliaryVisionDescriptionCache) describe(ctx context.Context, key string,
	load func(context.Context) (string, auxiliaryVisionUsage, error),
) (string, *auxiliaryVisionUsage, error) {
	for {
		if err := ctx.Err(); err != nil {
			return "", nil, err
		}
		cache.mu.Lock()
		if description, ok := cache.getLocked(key, time.Now()); ok {
			cache.mu.Unlock()
			return description, nil, nil
		}
		if flight := cache.flights[key]; flight != nil {
			cache.mu.Unlock()
			select {
			case <-ctx.Done():
				return "", nil, ctx.Err()
			case <-flight.done:
				// A canceled owner must not poison a live follower's request.
				if flight.retry {
					continue
				}
				return flight.description, nil, flight.err
			}
		}
		if cache.flights == nil {
			cache.flights = make(map[string]*auxiliaryVisionDescriptionFlight)
		}
		flight := &auxiliaryVisionDescriptionFlight{done: make(chan struct{})}
		cache.flights[key] = flight
		cache.mu.Unlock()

		description, usage, err := load(ctx)
		cache.mu.Lock()
		delete(cache.flights, key)
		flight.description, flight.err = description, err
		flight.retry = err != nil && ctx.Err() != nil
		if err == nil {
			cache.putLocked(key, description, time.Now())
		}
		close(flight.done)
		cache.mu.Unlock()
		if err != nil {
			return "", nil, err
		}
		return description, &usage, nil
	}
}

func (s *OpenAIGatewayService) describeAuxiliaryImageForRequest(ctx context.Context, c *gin.Context,
	account *Account, imageURL string,
) (string, *auxiliaryVisionUsage, error) {
	load := func(ctx context.Context) (string, auxiliaryVisionUsage, error) {
		return s.describeAuxiliaryImage(ctx, account, imageURL)
	}
	if key := auxiliaryVisionDescriptionCacheKey(c, account, imageURL); key != "" {
		return s.auxiliaryVisionDescriptions.describe(ctx, key, load)
	}
	description, usage, err := load(ctx)
	if err != nil {
		return "", nil, err
	}
	return description, &usage, nil
}
