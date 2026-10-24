package handler

import (
	"context"
	"errors"
	"testing"
	"time"

	"github.com/stretchr/testify/require"
)

func TestChatCheckpointSlowDatabaseCoalescesWithoutDelayingLiveTokens(t *testing.T) {
	started, release := make(chan string, 4), make(chan struct{})
	w := newChatCheckpointWriter(context.Background(), func(ctx context.Context, snapshot chatCheckpoint) error {
		started <- snapshot.content
		if snapshot.content == "first" {
			select {
			case <-release:
			case <-ctx.Done():
				return ctx.Err()
			}
		}
		return nil
	})
	defer w.stop()
	w.submit(chatCheckpoint{content: "first"})
	require.Equal(t, "first", <-started)
	live := newChatToolLive()
	queued := make(chan struct{})
	go func() {
		for _, text := range []string{"你", "你好", "你好🌟"} {
			w.submit(chatCheckpoint{content: text})
			live.publish(text, "", nil)
		}
		close(queued)
	}()
	select {
	case <-queued:
	case <-time.After(250 * time.Millisecond):
		t.Fatal("slow checkpoint blocked real model deltas")
	}
	snapshot, _ := live.snapshot()
	require.Equal(t, "你好🌟", snapshot.content)
	close(release)
	select {
	case text := <-started:
		require.Equal(t, "你好🌟", text, "only the newest queued checkpoint should be written")
	case <-time.After(time.Second):
		t.Fatal("latest checkpoint was lost")
	}
	require.Empty(t, started)
}

func TestChatCheckpointStopCancelsAndJoinsBeforeFinalization(t *testing.T) {
	started, exited := make(chan struct{}), make(chan struct{})
	w := newChatCheckpointWriter(context.Background(), func(ctx context.Context, _ chatCheckpoint) error {
		close(started)
		<-ctx.Done()
		close(exited)
		return ctx.Err()
	})
	w.submit(chatCheckpoint{content: "paid output"})
	<-started
	w.submit(chatCheckpoint{content: "queued output"})
	w.stop()
	select {
	case <-exited:
	default:
		t.Fatal("finalization could race an active checkpoint")
	}
	require.Empty(t, w.failed, "intentional stop must not become a save failure")
	w.stop() // Cleanup is idempotent.
}

func TestChatCheckpointFailureIsReportedToGeneration(t *testing.T) {
	want := errors.New("database unavailable")
	w := newChatCheckpointWriter(context.Background(), func(context.Context, chatCheckpoint) error { return want })
	defer w.stop()
	w.submit(chatCheckpoint{content: "partial"})
	select {
	case err := <-w.failed:
		require.ErrorIs(t, err, want)
	case <-time.After(time.Second):
		t.Fatal("checkpoint failure must not silently complete a generation")
	}
}
