package ratelimit

import (
	"context"
	"fmt"
	"os"
	"testing"
	"time"

	"github.com/redis/go-redis/v9"
)

// These need a real Redis, because the whole point is the Lua script running
// server-side. They skip without one:
//
//	docker compose up -d redis
//	CAREGRAPH_TEST_REDIS=localhost:6379 go test ./internal/ratelimit/
func testLimiter(t *testing.T) (*Limiter, string) {
	t.Helper()
	addr := os.Getenv("CAREGRAPH_TEST_REDIS")
	if addr == "" {
		t.Skip("CAREGRAPH_TEST_REDIS not set")
	}
	client := redis.NewClient(&redis.Options{Addr: addr})
	if err := client.Ping(context.Background()).Err(); err != nil {
		t.Fatalf("redis: %v", err)
	}
	t.Cleanup(func() { _ = client.Close() })

	// A bucket name unique to this test, so runs cannot interfere.
	bucket := fmt.Sprintf("test:%s:%d", t.Name(), time.Now().UnixNano())
	t.Cleanup(func() {
		_ = client.Del(context.Background(), "ratelimit:"+bucket).Err()
	})
	return New(client), bucket
}

func TestAllowsExactlyTheCapacityThenRefuses(t *testing.T) {
	limiter, bucket := testLimiter(t)
	ctx := context.Background()

	// 60/min refills one per second, so a burst finishes long before a token
	// returns and the count is exact.
	for i := 1; i <= 60; i++ {
		d, err := limiter.Allow(ctx, bucket, 60)
		if err != nil {
			t.Fatalf("request %d: %v", i, err)
		}
		if !d.Allowed {
			t.Fatalf("request %d refused, expected the first 60 to pass", i)
		}
	}

	d, err := limiter.Allow(ctx, bucket, 60)
	if err != nil {
		t.Fatal(err)
	}
	if d.Allowed {
		t.Error("request 61 was allowed — capacity is not enforced")
	}
	if d.RetryAfter <= 0 {
		t.Errorf("RetryAfter = %v, want a positive hint", d.RetryAfter)
	}
}

func TestRemainingCountsDown(t *testing.T) {
	limiter, bucket := testLimiter(t)
	ctx := context.Background()

	first, err := limiter.Allow(ctx, bucket, 100)
	if err != nil {
		t.Fatal(err)
	}
	second, err := limiter.Allow(ctx, bucket, 100)
	if err != nil {
		t.Fatal(err)
	}
	if second.Remaining >= first.Remaining {
		t.Errorf("remaining did not decrease: %d then %d", first.Remaining, second.Remaining)
	}
}

func TestPeekDoesNotSpendATokenButReportsExhaustion(t *testing.T) {
	limiter, bucket := testLimiter(t)
	ctx := context.Background()

	before, err := limiter.Peek(ctx, bucket, 5)
	if err != nil {
		t.Fatal(err)
	}
	if !before.Allowed {
		t.Fatal("a fresh bucket reported exhausted")
	}
	for range 10 {
		if _, err := limiter.Peek(ctx, bucket, 5); err != nil {
			t.Fatal(err)
		}
	}
	// Ten peeks must not have consumed the budget — this is what keeps a
	// successful authentication from depleting the failed-auth brake.
	after, err := limiter.Allow(ctx, bucket, 5)
	if err != nil {
		t.Fatal(err)
	}
	if !after.Allowed || after.Remaining != 4 {
		t.Errorf("after 11 peeks: allowed=%v remaining=%d, want true and 4",
			after.Allowed, after.Remaining)
	}

	// Drain, then peek must report exhaustion.
	for range 4 {
		if _, err := limiter.Allow(ctx, bucket, 5); err != nil {
			t.Fatal(err)
		}
	}
	drained, err := limiter.Peek(ctx, bucket, 5)
	if err != nil {
		t.Fatal(err)
	}
	if drained.Allowed {
		t.Error("peek reported a drained bucket as allowed")
	}
}

func TestSpendChargesEvenPastTheLimit(t *testing.T) {
	limiter, bucket := testLimiter(t)
	ctx := context.Background()

	for range 3 {
		if err := limiter.Spend(ctx, bucket, 2); err != nil {
			t.Fatal(err)
		}
	}
	d, err := limiter.Peek(ctx, bucket, 2)
	if err != nil {
		t.Fatal(err)
	}
	if d.Allowed {
		t.Error("bucket should be exhausted after spending past its capacity")
	}
}

func TestBucketRefillsOverTime(t *testing.T) {
	limiter, bucket := testLimiter(t)
	ctx := context.Background()

	// 60/min = one token per second. Deliberately slow: at a high rate the bucket
	// refills measurably *while* it is being drained — 600 round trips take about
	// 120 ms and hand back a token before the drain finishes, which is correct
	// behaviour but makes "is it empty now" untestable.
	const perMinute = 60
	for i := range perMinute {
		if _, err := limiter.Allow(ctx, bucket, perMinute); err != nil {
			t.Fatalf("request %d: %v", i, err)
		}
	}
	if d, _ := limiter.Allow(ctx, bucket, perMinute); d.Allowed {
		t.Fatal("bucket was not exhausted")
	}

	time.Sleep(1200 * time.Millisecond)

	d, err := limiter.Allow(ctx, bucket, perMinute)
	if err != nil {
		t.Fatal(err)
	}
	if !d.Allowed {
		t.Error("bucket did not refill after a token's worth of time had passed")
	}
}

func TestBucketsAreIndependent(t *testing.T) {
	limiter, bucket := testLimiter(t)
	ctx := context.Background()

	for range 5 {
		if _, err := limiter.Allow(ctx, bucket, 5); err != nil {
			t.Fatal(err)
		}
	}
	other, err := limiter.Allow(ctx, bucket+":other", 5)
	if err != nil {
		t.Fatal(err)
	}
	if !other.Allowed {
		t.Error("one client's exhausted bucket blocked another")
	}
}

func TestNonPositiveLimitIsAnError(t *testing.T) {
	limiter, bucket := testLimiter(t)
	for _, perMinute := range []int{0, -1} {
		if _, err := limiter.Allow(context.Background(), bucket, perMinute); err == nil {
			t.Errorf("limit %d was accepted — a zero limit would divide by zero in Lua", perMinute)
		}
	}
}

func TestErrorNeverReportsAllowed(t *testing.T) {
	// An unreachable Redis must not produce a permissive Decision; the caller
	// decides how to treat the error, and a zero value that said "allowed" would
	// make failing open the accidental default everywhere.
	limiter := New(redis.NewClient(&redis.Options{Addr: "127.0.0.1:1"}))
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()

	d, err := limiter.Allow(ctx, "unreachable", 10)
	if err == nil {
		t.Fatal("expected an error from an unreachable redis")
	}
	if d.Allowed {
		t.Error("Decision.Allowed was true alongside an error")
	}
}
