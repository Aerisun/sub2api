package repository

import "github.com/redis/go-redis/v9"

// Keep the slot and its cleanup index consistent in one Redis round trip.
var acquireIndexedSlotScript = redis.NewScript(`
 redis.replicate_commands()
 local now = tonumber(redis.call('TIME')[1])
 local cap, ttl, requestID = tonumber(ARGV[1]), tonumber(ARGV[2]), ARGV[3]
 redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now-ttl)
 redis.call('ZREMRANGEBYSCORE', KEYS[2], '-inf', now-60)
 if redis.call('ZSCORE', KEYS[1], requestID) == false then
  if redis.call('ZCARD', KEYS[1]) + redis.call('ZCARD', KEYS[2]) >= cap then return 0 end
 end
 redis.call('ZADD', KEYS[1], now, requestID)
 redis.call('EXPIRE', KEYS[1], ttl)
 redis.call('ZADD', KEYS[3], now+ttl, ARGV[4])
 return 1
`)

var releaseIndexedSlotScript = redis.NewScript(`
 redis.replicate_commands()
 local now = tonumber(redis.call('TIME')[1])
 local slotTTL, waitTTL = tonumber(ARGV[3]), tonumber(ARGV[4])
 redis.call('ZREM', KEYS[1], ARGV[1])
 redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now-slotTTL)
 redis.call('ZREMRANGEBYSCORE', KEYS[2], '-inf', now-60)
 local count = redis.call('ZCARD', KEYS[1]) + redis.call('ZCARD', KEYS[2])
 local waiting = tonumber(redis.call('GET', KEYS[3]) or '0')
 if count == 0 and waiting <= 0 then
  redis.call('ZREM', KEYS[4], ARGV[2])
 else
  local ttl = 0
  if count > 0 then ttl = slotTTL end
  if waiting > 0 then ttl = math.max(ttl, waitTTL) end
  redis.call('ZADD', KEYS[4], now+ttl, ARGV[2])
 end
 return 1
`)
