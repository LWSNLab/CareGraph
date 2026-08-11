// Package ratelimit implements a Redis-backed token bucket, shared by every
// gateway instance so a limit means the same thing behind a load balancer.
package ratelimit

import (
	"context"
	"errors"
	"fmt"
	"math"
	"time"

	"github.com/redis/go-redis/v9"
)

// tokenBucket refills continuously and is evaluated atomically inside Redis.
//
// A Lua script rather than INCR + EXPIRE: a fixed window lets a client spend its
// whole quota at the end of one window and again at the start of the next, so a
// "100 per minute" limit permits 200 requests in a couple of seconds. A bucket
// has no such edge, and doing the read-modify-write in one script removes the
// race between concurrent requests that INCR alone cannot cover for a bucket.
//
// Returns {allowed, remaining, retry_after_seconds}.
var tokenBucket = redis.NewScript(`
local key      = KEYS[1]
local capacity = tonumber(ARGV[1])
local refill   = tonumber(ARGV[2])   -- tokens per second
local now      = tonumber(ARGV[3])   -- unix seconds, fractional
local cost     = tonumber(ARGV[4])

-- cost 0 is a peek: refill and report whether a token is available, without
-- spending one. Lets a caller check a budget before doing expensive work and
-- charge it only if that work turns out to have been wasted.
local state  = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(state[1])
local ts     = tonumber(state[2])

if tokens == nil or ts == nil then
  tokens = capacity
  ts     = now
end

-- max(0, ...) guards against a clock that moved backwards between instances.
local elapsed = math.max(0, now - ts)
tokens = math.min(capacity, tokens + elapsed * refill)

local allowed = 0
if cost == 0 then
  -- Peek: a budget with nothing left is not allowed, but nothing is spent.
  if tokens >= 1 then allowed = 1 end
elseif tokens >= cost then
  tokens  = tokens - cost
  allowed = 1
end

redis.call('HSET', key, 'tokens', tokens, 'ts', now)
-- Live only as long as it takes to refill from empty: an idle client's bucket is
-- indistinguishable from a full one, so keeping it wastes memory.
redis.call('PEXPIRE', key, math.ceil((capacity / refill) * 1000) + 1000)

local retry = 0
if allowed == 0 then
  retry = math.ceil((math.max(cost, 1) - tokens) / refill)
end
return {allowed, math.floor(tokens), retry}
`)

// Decision is the outcome of one limit check.
type Decision struct {
	Allowed    bool
	Remaining  int
	RetryAfter time.Duration
}

// Limiter checks buckets in Redis.
type Limiter struct {
	client redis.Scripter
}

// New wires a limiter over a Redis client.
func New(client redis.Scripter) *Limiter {
	return &Limiter{client: client}
}

// Allow spends one token from the named bucket.
//
// An error means the limiter could not reach a verdict — the caller decides
// whether that permits or denies the request. It never returns Allowed on error.
func (l *Limiter) Allow(ctx context.Context, bucket string, perMinute int) (Decision, error) {
	return l.run(ctx, bucket, perMinute, 1)
}

// Peek reports whether the bucket has a token, without spending one.
//
// For budgets that should only be charged when work turns out to be wasted: a
// failed authentication costs a token, a successful one does not, so legitimate
// traffic never depletes the budget that exists to stop abuse.
func (l *Limiter) Peek(ctx context.Context, bucket string, perMinute int) (Decision, error) {
	return l.run(ctx, bucket, perMinute, 0)
}

// Spend takes one token and ignores whether the bucket had any left. Used to
// charge for an event that already happened.
func (l *Limiter) Spend(ctx context.Context, bucket string, perMinute int) error {
	_, err := l.run(ctx, bucket, perMinute, 1)
	return err
}

func (l *Limiter) run(ctx context.Context, bucket string, perMinute, cost int) (Decision, error) {
	if perMinute <= 0 {
		return Decision{}, fmt.Errorf("rate limit must be positive, got %d", perMinute)
	}

	capacity := float64(perMinute)
	refill := capacity / 60.0
	now := float64(time.Now().UnixNano()) / float64(time.Second)

	raw, err := tokenBucket.Run(ctx, l.client, []string{"ratelimit:" + bucket},
		capacity, refill, now, cost).Slice()
	if err != nil {
		return Decision{}, fmt.Errorf("token bucket %q: %w", bucket, err)
	}
	if len(raw) != 3 {
		return Decision{}, fmt.Errorf("token bucket %q: unexpected reply %v", bucket, raw)
	}

	allowed, ok1 := raw[0].(int64)
	remaining, ok2 := raw[1].(int64)
	retry, ok3 := raw[2].(int64)
	if !ok1 || !ok2 || !ok3 {
		return Decision{}, fmt.Errorf("token bucket %q: unexpected reply types %v", bucket, raw)
	}

	return Decision{
		Allowed:    allowed == 1,
		Remaining:  int(math.Max(0, float64(remaining))),
		RetryAfter: time.Duration(retry) * time.Second,
	}, nil
}

// Ping reports whether Redis is reachable, for a readiness probe.
func (l *Limiter) Ping(ctx context.Context) error {
	pinger, ok := l.client.(interface {
		Ping(context.Context) *redis.StatusCmd
	})
	if !ok {
		return errors.New("client does not support ping")
	}
	return pinger.Ping(ctx).Err()
}
