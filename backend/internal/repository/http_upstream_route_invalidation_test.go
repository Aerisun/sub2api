package repository

import (
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/Wei-Shaw/sub2api/internal/pkg/tlsfingerprint"
	"github.com/Wei-Shaw/sub2api/internal/service"
	"github.com/stretchr/testify/require"
)

func TestHTTPUpstreamRouteInvalidationPreservesAcceptedStream(t *testing.T) {
	finish := make(chan struct{})
	release := sync.OnceFunc(func() { close(finish) })
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("X-Connection", r.RemoteAddr)
		if r.URL.Path == "/stream" {
			_, _ = io.WriteString(w, "begin\n")
			_ = http.NewResponseController(w).Flush()
			select {
			case <-finish:
			case <-r.Context().Done():
				return
			}
		}
		_, _ = io.WriteString(w, "complete\n")
	}))
	defer srv.Close()
	defer release()
	s, ok := NewHTTPUpstream(nil).(*httpUpstreamService)
	require.True(t, ok)
	ctx, cancel := context.WithTimeout(t.Context(), 5*time.Second)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, srv.URL+"/stream", nil)
	require.NoError(t, err)
	first, err := s.Do(req, "", 1, 1)
	require.NoError(t, err)
	defer func() { _ = first.Body.Close() }()
	prefix := make([]byte, 6)
	_, err = io.ReadFull(first.Body, prefix)
	require.NoError(t, err)
	old, err := s.getOrCreateClient("", 1, 1)
	require.NoError(t, err)
	other, err := s.getOrCreateClient("", 2, 1)
	require.NoError(t, err)
	s.InvalidateConnectionPool("", 1)
	require.True(t, old.retired.Load())
	require.False(t, other.retired.Load(), "another account must keep its transport")
	req, err = http.NewRequestWithContext(ctx, http.MethodGet, srv.URL+"/new", nil)
	require.NoError(t, err)
	second, err := s.Do(req, "", 1, 1)
	require.NoError(t, err)
	defer func() { _ = second.Body.Close() }()
	require.NotEqual(t, first.Header.Get("X-Connection"), second.Header.Get("X-Connection"))
	fresh, err := s.getOrCreateClient("", 1, 1)
	require.NoError(t, err)
	require.NotSame(t, old, fresh)
	release()
	remainder, err := io.ReadAll(first.Body)
	require.NoError(t, err, "retiring a pool must not cancel accepted generation")
	require.Equal(t, "complete\n", string(remainder))
	require.NoError(t, first.Body.Close())
	require.Zero(t, atomic.LoadInt64(&old.inFlight))
}

func TestHTTPUpstreamRouteInvalidationCoversProfilesAndIsolation(t *testing.T) {
	for _, isolation := range []string{config.ConnectionPoolIsolationAccount, config.ConnectionPoolIsolationAccountProxy, config.ConnectionPoolIsolationProxy} {
		t.Run(isolation, func(t *testing.T) {
			cfg := &config.Config{}
			cfg.Gateway.ConnectionPoolIsolation = isolation
			s, ok := NewHTTPUpstream(cfg).(*httpUpstreamService)
			require.True(t, ok)
			proxy := "http://127.0.0.1:17991"
			plain, err := s.getClientEntry(proxy, 1, 4, service.HTTPUpstreamProfileDefault, false, false)
			require.NoError(t, err)
			tlsEntry, err := s.getClientEntryWithTLS(proxy, 1, 4, &tlsfingerprint.Profile{Name: "fixture"}, service.HTTPUpstreamProfileDefault, false, false)
			require.NoError(t, err)
			unrelated, err := s.getClientEntry("http://127.0.0.1:17995", 2, 4, service.HTTPUpstreamProfileDefault, false, false)
			require.NoError(t, err)
			s.InvalidateConnectionPool(proxy, 1)
			require.True(t, plain.retired.Load())
			require.True(t, tlsEntry.retired.Load())
			require.False(t, unrelated.retired.Load())
		})
	}
}
