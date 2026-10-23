package repository

import (
	"context"
	"github.com/redis/go-redis/v9"
	"strconv"
)

const openAIUserSlotKeyPrefix = "concurrency:user_openai:"
const liveOpenAIUserSlotKeyPrefix = "concurrency:live:user_openai:"

func openAIUserSlotKey(id int64) string { return openAIUserSlotKeyPrefix + strconv.FormatInt(id, 10) }
func liveOpenAIUserSlotKey(id int64) string {
	return liveOpenAIUserSlotKeyPrefix + strconv.FormatInt(id, 10)
}

// A request is admitted to BOTH budgets or neither. Waiting for provider
// capacity must not consume the user's global capacity in the meantime.
var acquireOpenAIUserSlotScript = redis.NewScript(`
    redis.replicate_commands()
    local now = tonumber(redis.call('TIME')[1])
    local globalMax, providerMax, ttl = tonumber(ARGV[1]), tonumber(ARGV[2]), tonumber(ARGV[3])
    local requestID, userID = ARGV[4], ARGV[5]
    redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now-ttl)
    redis.call('ZREMRANGEBYSCORE', KEYS[2], '-inf', now-60)
    redis.call('ZREMRANGEBYSCORE', KEYS[3], '-inf', now-ttl)
    redis.call('ZREMRANGEBYSCORE', KEYS[4], '-inf', now-60)
    if redis.call('ZSCORE', KEYS[1], requestID) == false then
        local globalCount = redis.call('ZCARD', KEYS[1]) + redis.call('ZCARD', KEYS[2])
        local providerCount = redis.call('ZCARD', KEYS[3]) + redis.call('ZCARD', KEYS[4])
        if globalMax > 0 and globalCount >= globalMax then return {0, now} end
        if providerMax > 0 and providerCount >= providerMax then return {0, now} end
    end
    redis.call('ZADD', KEYS[1], now, requestID)
    redis.call('ZADD', KEYS[3], now, requestID)
    redis.call('EXPIRE', KEYS[1], ttl)
    redis.call('EXPIRE', KEYS[3], ttl)
    redis.call('ZADD', KEYS[5], now+ttl, userID)
    return {1, now}
`)

// Release and repair the active index in one Redis round trip. Removing a
// provider member that was never added is harmless for non-OpenAI requests.
var releaseUserSlotsScript = redis.NewScript(`
    redis.replicate_commands()
    redis.call('ZREM', KEYS[1], ARGV[1])
    redis.call('ZREM', KEYS[2], ARGV[1])
    local count = redis.call('ZCARD', KEYS[1]) + redis.call('ZCARD', KEYS[3])
    local waiting = tonumber(redis.call('GET', KEYS[4]) or '0')
    if count == 0 and waiting <= 0 then
        redis.call('ZREM', KEYS[5], ARGV[2])
    else
        local now = tonumber(redis.call('TIME')[1])
        redis.call('ZADD', KEYS[5], now+tonumber(ARGV[3]), ARGV[2])
    end
    return 1
`)

func (c *concurrencyCache) GetUserOpenAIConcurrencyBatch(ctx context.Context, ids []int64) (map[int64]int, error) {
	result := make(map[int64]int, len(ids))
	if len(ids) == 0 {
		return result, nil
	}
	now, err := c.redisUnixSeconds(ctx)
	if err != nil {
		return nil, err
	}
	type counts struct {
		id            int64
		regular, live *redis.IntCmd
	}
	reads := make([]counts, 0, len(ids))
	_, err = c.rdb.Pipelined(ctx, func(pipe redis.Pipeliner) error {
		for _, id := range ids {
			if id <= 0 {
				continue
			}
			regular, live := openAIUserSlotKey(id), liveOpenAIUserSlotKey(id)
			pipe.ZRemRangeByScore(ctx, regular, "-inf", strconv.FormatInt(now-int64(c.slotTTLSeconds), 10))
			pipe.ZRemRangeByScore(ctx, live, "-inf", strconv.FormatInt(now-liveLeaseTTLSeconds, 10))
			reads = append(reads, counts{id, pipe.ZCard(ctx, regular), pipe.ZCard(ctx, live)})
		}
		return nil
	})
	if err != nil {
		return nil, err
	}
	for _, read := range reads {
		result[read.id] = int(read.regular.Val() + read.live.Val())
	}
	return result, nil
}
