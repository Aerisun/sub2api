package handler

import (
	"github.com/Wei-Shaw/sub2api/internal/service"
	"sync"
)

// Text, reasoning and tools reach active readers without waiting for a database
// checkpoint. The snapshot shares immutable strings with the bounded worker.
// Readers still authorize ownership through the database before subscribing.
type chatToolLive struct {
	mu      sync.Mutex
	state   chatLiveSnapshot
	changed chan struct{}
}

type chatLiveSnapshot struct {
	content, reasoning string
	tools              []service.InternalToolEvent
	version            uint64
	finished           bool
	imageRequest       *chatImageRequest
}

func newChatToolLive() *chatToolLive { return &chatToolLive{changed: make(chan struct{})} }
func (live *chatToolLive) publish(content, reasoning string, tools []service.InternalToolEvent, requests ...*chatImageRequest) {
	live.mu.Lock()
	defer live.mu.Unlock()
	live.state.content, live.state.reasoning = content, reasoning
	live.state.tools = append([]service.InternalToolEvent{}, tools...)
	if len(requests) > 0 {
		live.state.imageRequest = requests[0]
	}
	live.state.version++
	close(live.changed)
	live.changed = make(chan struct{})
}
func (live *chatToolLive) finish() {
	live.mu.Lock()
	defer live.mu.Unlock()
	live.state.finished = true
	close(live.changed)
}
func (live *chatToolLive) snapshot() (chatLiveSnapshot, <-chan struct{}) {
	live.mu.Lock()
	defer live.mu.Unlock()
	snapshot := live.state
	snapshot.tools = append([]service.InternalToolEvent{}, snapshot.tools...)
	return snapshot, live.changed
}

// Reconnection offsets count Unicode characters, like PostgreSQL substring.
// Convert once, then advance by bytes to avoid rescanning a growing reply.
type chatStreamCursor struct {
	runes, bytes int
}

func newChatStreamCursor(offset int) chatStreamCursor {
	return chatStreamCursor{runes: offset, bytes: -1}
}
func (cursor *chatStreamCursor) suffix(text string) string {
	if cursor.bytes < 0 {
		remaining := cursor.runes
		cursor.bytes = len(text)
		for at := range text {
			if remaining == 0 {
				cursor.bytes = at
				break
			}
			remaining--
		}
		if remaining > 0 {
			cursor.bytes = -1
			return ""
		}
	}
	if cursor.bytes > len(text) {
		return ""
	}
	delta := text[cursor.bytes:]
	cursor.bytes = len(text)
	for range delta {
		cursor.runes++
	}
	return delta
}
