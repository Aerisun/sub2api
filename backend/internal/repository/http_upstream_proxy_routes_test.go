package repository

import (
	"context"
	"crypto/tls"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/Wei-Shaw/sub2api/internal/pkg/tlsfingerprint"
	"github.com/Wei-Shaw/sub2api/internal/service"
	"github.com/stretchr/testify/require"
)

func writeProxyRouteFixture(t *testing.T, path string, routes map[string]string) {
	t.Helper()
	raw, err := json.Marshal(proxyRouteSnapshot{Version: 1, Routes: routes})
	require.NoError(t, err)
	require.NoError(t, os.WriteFile(path+".pending", raw, 0600))
	require.NoError(t, os.Rename(path+".pending", path))
}

// Exercise an actual multiplexed H2 connection through a local SOCKS tunnel.
// A second request must reuse the warm connection until its route changes,
// then open a new tunnel while the first response is still being generated.
func TestProxyRouteGenerationReroutesHTTP2WithoutCancellingStream(t *testing.T) {
	finish := make(chan struct{})
	release := sync.OnceFunc(func() { close(finish) })
	srv := httptest.NewUnstartedServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
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
	srv.EnableHTTP2 = true
	srv.StartTLS()
	defer srv.Close()
	defer release()
	proxyURL, tunnels := startTestSOCKS5Proxy(t)
	path := filepath.Join(t.TempDir(), "routes.json")
	writeProxyRouteFixture(t, path, map[string]string{proxyURL: strings.Repeat("a", 64)})
	cfg := &config.Config{}
	cfg.Gateway.ProxyRouteStateFile = path
	cfg.Gateway.OpenAIHTTP2.Enabled = true
	s := NewHTTPUpstream(cfg).(*httpUpstreamService)
	defer s.proxyRoutes.close()
	ctx, cancel := context.WithTimeout(t.Context(), 8*time.Second)
	defer cancel()
	ctx = service.WithHTTPUpstreamProfile(ctx, service.HTTPUpstreamProfileOpenAI)
	request := func(path string) *http.Response {
		t.Helper()
		entry, err := s.getClientEntry(proxyURL, 1, 4, service.HTTPUpstreamProfileOpenAI, false, false)
		require.NoError(t, err)
		transport := entry.client.Transport.(*http.Transport)
		if transport.TLSClientConfig == nil {
			transport.TLSClientConfig = &tls.Config{InsecureSkipVerify: true} // Local test certificate only.
		} else if !transport.TLSClientConfig.InsecureSkipVerify {
			transport.TLSClientConfig = transport.TLSClientConfig.Clone()
			transport.TLSClientConfig.InsecureSkipVerify = true // Local test certificate only.
		}
		req, err := http.NewRequestWithContext(ctx, http.MethodGet, srv.URL+path, nil)
		require.NoError(t, err)
		response, err := s.Do(req, proxyURL, 1, 4)
		require.NoError(t, err)
		t.Cleanup(func() { _ = response.Body.Close() })
		require.Equal(t, 2, response.ProtoMajor)
		return response
	}
	first := request("/stream")
	prefix := make([]byte, 6)
	_, err := io.ReadFull(first.Body, prefix)
	require.NoError(t, err)
	require.Equal(t, "begin\n", string(prefix))
	old, err := s.getClientEntry(proxyURL, 1, 4, service.HTTPUpstreamProfileOpenAI, false, false)
	require.NoError(t, err)
	warm := request("/warm")
	_, err = io.Copy(io.Discard, warm.Body)
	require.NoError(t, err)
	require.NoError(t, warm.Body.Close())
	require.Equal(t, first.Header.Get("X-Connection"), warm.Header.Get("X-Connection"))
	require.EqualValues(t, 1, tunnels.Load())
	writeProxyRouteFixture(t, path, map[string]string{proxyURL: strings.Repeat("b", 64)})
	s.proxyRoutes.refresh()
	fresh := request("/new-route")
	_, err = io.Copy(io.Discard, fresh.Body)
	require.NoError(t, err)
	require.NoError(t, fresh.Body.Close())
	require.NotEqual(t, first.Header.Get("X-Connection"), fresh.Header.Get("X-Connection"))
	require.EqualValues(t, 2, tunnels.Load())
	require.True(t, old.retired.Load())
	require.EqualValues(t, 1, atomic.LoadInt64(&old.inFlight))
	stillWarm := request("/same-route")
	_, err = io.Copy(io.Discard, stillWarm.Body)
	require.NoError(t, err)
	require.NoError(t, stillWarm.Body.Close())
	require.Equal(t, fresh.Header.Get("X-Connection"), stillWarm.Header.Get("X-Connection"))
	require.EqualValues(t, 2, tunnels.Load(), "unchanged route should keep its warm tunnel")
	release()
	remainder, err := io.ReadAll(first.Body)
	require.NoError(t, err, "route rollover must preserve the accepted response")
	require.Equal(t, "complete\n", string(remainder))
	require.NoError(t, first.Body.Close())
	require.Zero(t, atomic.LoadInt64(&old.inFlight))
}

func TestProxyRouteGenerationPreservesOtherRolesAndCoversTLS(t *testing.T) {
	for _, isolation := range []string{config.ConnectionPoolIsolationAccount, config.ConnectionPoolIsolationAccountProxy, config.ConnectionPoolIsolationProxy} {
		t.Run(isolation, func(t *testing.T) {
			path := filepath.Join(t.TempDir(), "routes.json")
			proxy, otherProxy := "http://127.0.0.1:17995", "http://127.0.0.1:17991"
			writeProxyRouteFixture(t, path, map[string]string{proxy: strings.Repeat("a", 64), otherProxy: strings.Repeat("b", 64)})
			cfg := &config.Config{}
			cfg.Gateway.ConnectionPoolIsolation, cfg.Gateway.ProxyRouteStateFile = isolation, path
			s := NewHTTPUpstream(cfg).(*httpUpstreamService)
			defer s.proxyRoutes.close()
			entry, err := s.getOrCreateClient(proxy, 1, 4)
			require.NoError(t, err)
			profile := &tlsfingerprint.Profile{Name: "fixture"}
			tlsEntry, err := s.getClientEntryWithTLS(proxy, 1, 4, profile, service.HTTPUpstreamProfileDefault, false, false)
			require.NoError(t, err)
			unrelated, err := s.getOrCreateClient(otherProxy, 2, 4)
			require.NoError(t, err)
			writeProxyRouteFixture(t, path, map[string]string{proxy: strings.Repeat("c", 64)})
			s.proxyRoutes.refresh()
			next, err := s.getOrCreateClient(proxy, 1, 4)
			require.NoError(t, err)
			nextTLS, err := s.getClientEntryWithTLS(proxy, 1, 4, profile, service.HTTPUpstreamProfileDefault, false, false)
			require.NoError(t, err)
			stillUnrelated, err := s.getOrCreateClient(otherProxy, 2, 4)
			require.NoError(t, err)
			require.NotSame(t, entry, next)
			require.NotSame(t, tlsEntry, nextTLS)
			require.True(t, entry.retired.Load())
			require.True(t, tlsEntry.retired.Load())
			require.Same(t, unrelated, stillUnrelated)
		})
	}
}

func TestProxyRouteGenerationInvalidSnapshotsKeepLastGoodRoute(t *testing.T) {
	path := filepath.Join(t.TempDir(), "routes.json")
	proxy := "http://127.0.0.1:17995"
	writeProxyRouteFixture(t, path, map[string]string{proxy: strings.Repeat("a", 64)})
	r := newProxyRouteGenerations(path)
	defer r.close()
	good := r.current.Load()
	for _, raw := range []string{"{", `{"version":2,"routes":{}}`, `{"version":1,"routes":{}}`,
		`{"version":1,"routes":{"http://127.0.0.1:17995":"invalid"}}`,
		`{"version":1,"routes":{"":"` + strings.Repeat("b", 64) + `"}}`, strings.Repeat(" ", maxProxyRouteStateBytes+1)} {
		require.NoError(t, os.WriteFile(path, []byte(raw), 0600))
		r.refresh()
		require.Same(t, good, r.current.Load())
	}
	require.NoError(t, os.Remove(path))
	r.refresh()
	require.Same(t, good, r.current.Load())
	writeProxyRouteFixture(t, path, map[string]string{proxy: strings.Repeat("b", 64)})
	require.Eventually(t, func() bool {
		return r.current.Load().Routes[proxy] == strings.Repeat("b", 64)
	}, 2*time.Second, 10*time.Millisecond, "background watcher should adopt an atomic replacement")
}
