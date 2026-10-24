package handler

import (
	"context"
	"time"
)

type chatCheckpoint struct {
	content, reasoning string
	tools              []byte
}

// One writer owns checkpoint ordering. A slow database can retain at most one
// pending snapshot, rather than blocking model reads or queuing every token.
// Strings and serialized tools are immutable snapshots owned by the caller.
type chatCheckpointWriter struct {
	pending chan chatCheckpoint
	failed  chan error
	done    chan struct{}
	cancel  context.CancelFunc
}

func newChatCheckpointWriter(parent context.Context, save func(context.Context, chatCheckpoint) error) *chatCheckpointWriter {
	ctx, cancel := context.WithCancel(parent)
	w := &chatCheckpointWriter{
		pending: make(chan chatCheckpoint, 1), failed: make(chan error, 1),
		done: make(chan struct{}), cancel: cancel,
	}
	go func() {
		defer close(w.done)
		for {
			select {
			case <-ctx.Done():
				return
			case snapshot := <-w.pending:
				if ctx.Err() != nil {
					return
				}
				saveCtx, stop := context.WithTimeout(ctx, 10*time.Second)
				err := save(saveCtx, snapshot)
				stop()
				if err != nil {
					if ctx.Err() == nil {
						w.failed <- err
					}
					return
				}
			}
		}
	}()
	return w
}

// submit has one producer (the generation loop). Replacing a queued snapshot
// never interrupts the write already in flight and never waits for the database.
func (w *chatCheckpointWriter) submit(snapshot chatCheckpoint) {
	select {
	case w.pending <- snapshot:
		return
	default:
	}
	select {
	case <-w.pending:
	default:
	}
	w.pending <- snapshot
}

// Join before finalization, so a late checkpoint cannot race a stopped or
// completed transcript. Finalization persists the complete current buffers.
func (w *chatCheckpointWriter) stop() {
	w.cancel()
	<-w.done
}
