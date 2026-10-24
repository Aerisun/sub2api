package handler

import (
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/stretchr/testify/require"
)

func TestChatTransportReusesConsumedResponsesWithoutMixingIdentity(t *testing.T) {
	var mu sync.Mutex
	var addresses, credentials, bodies []string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		mu.Lock()
		addresses = append(addresses, r.RemoteAddr)
		credentials = append(credentials, r.Header.Get("Authorization"))
		bodies = append(bodies, string(body))
		mu.Unlock()
		_, _ = io.WriteString(w, "OK")
	}))
	defer server.Close()
	client := newChatHTTPClient()
	defer client.CloseIdleConnections()
	for _, identity := range []string{"first", "second"} {
		request, err := http.NewRequest(http.MethodPost, server.URL, strings.NewReader(identity))
		require.NoError(t, err)
		request.Header.Set("Authorization", identity)
		response, err := doChatRequest(client, request)
		require.NoError(t, err)
		body, err := io.ReadAll(response.Body)
		require.NoError(t, err)
		require.Equal(t, "OK", string(body))
		require.NoError(t, response.Body.Close())
	}
	mu.Lock()
	defer mu.Unlock()
	require.Equal(t, addresses[0], addresses[1])
	require.Equal(t, []string{"first", "second"}, credentials)
	require.Equal(t, credentials, bodies)
}

func TestChatTransportTerminalBeforeHTTPEOFDoesNotHoldNextCaller(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/held" {
			w.Header().Set("Content-Type", "text/event-stream")
			_, _ = io.WriteString(w, "data: [DONE]\n\n")
			flusher, ok := w.(http.Flusher)
			if !ok {
				return
			}
			flusher.Flush()
			<-r.Context().Done()
			return
		}
		_, _ = io.WriteString(w, "next")
	}))
	defer server.Close()
	client := newChatHTTPClient()
	transport, ok := client.Transport.(*http.Transport)
	require.True(t, ok)
	transport.MaxConnsPerHost = 1
	defer client.CloseIdleConnections()
	request, _ := http.NewRequest(http.MethodPost, server.URL+"/held", strings.NewReader("paid"))
	response, err := doChatRequest(client, request)
	require.NoError(t, err)
	raw := make([]byte, len("data: [DONE]\n\n"))
	_, err = io.ReadFull(response.Body, raw)
	require.NoError(t, err)
	require.NoError(t, response.Body.Close())
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	request, _ = http.NewRequestWithContext(ctx, http.MethodPost, server.URL, strings.NewReader("next"))
	response, err = doChatRequest(client, request)
	require.NoError(t, err, "the previous early close stranded a pooled request")
	defer func() { _ = response.Body.Close() }()
	body, err := io.ReadAll(response.Body)
	require.NoError(t, err)
	require.Equal(t, "next", string(body))
}

func TestChatTransportDoesNotReplayPaidPOSTAfterReusedSocketBreaks(t *testing.T) {
	var mu sync.Mutex
	calls := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = io.Copy(io.Discard, r.Body)
		mu.Lock()
		calls++
		current := calls
		mu.Unlock()
		if current == 2 {
			hijacker, ok := w.(http.Hijacker)
			if !ok {
				return
			}
			connection, _, err := hijacker.Hijack()
			if err == nil {
				_ = connection.Close()
			}
			return
		}
		_, _ = io.WriteString(w, "OK")
	}))
	defer server.Close()
	client := newChatHTTPClient()
	defer client.CloseIdleConnections()
	for i := 0; i < 2; i++ {
		request, _ := http.NewRequest(http.MethodPost, server.URL, strings.NewReader("original paid prompt"))
		request.Header.Set("Idempotency-Key", "durable-chat-turn")
		response, err := doChatRequest(client, request)
		if i == 1 {
			require.Error(t, err, "an ambiguous generation must not be silently repeated")
			break
		}
		require.NoError(t, err)
		_, err = io.Copy(io.Discard, response.Body)
		require.NoError(t, err)
		require.NoError(t, response.Body.Close())
	}
	mu.Lock()
	defer mu.Unlock()
	require.Equal(t, 2, calls)
}
