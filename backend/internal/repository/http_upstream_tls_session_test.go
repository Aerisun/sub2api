package repository

import (
	"context"
	"crypto/x509"
	"io"
	"net/http"
	"net/http/httptest"
	"net/http/httptrace"
	"testing"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/Wei-Shaw/sub2api/internal/service"
	"github.com/stretchr/testify/require"
)

func TestUpstreamTLSResumesAfterProviderClosesSocket(t *testing.T) {
	resumed := make(chan bool, 2)
	server := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		resumed <- r.TLS.DidResume
		w.Header().Set("Connection", "close")
		_, _ = io.WriteString(w, "complete")
	}))
	defer server.Close()
	transport, err := buildUpstreamTransport(defaultPoolSettings(nil), nil, upstreamProtocolModeDefault)
	require.NoError(t, err)
	defer transport.CloseIdleConnections()
	roots := x509.NewCertPool()
	roots.AddCert(server.Certificate())
	transport.TLSClientConfig.RootCAs = roots
	client := &http.Client{Transport: transport}
	for range 2 {
		response, err := client.Get(server.URL)
		require.NoError(t, err)
		body, err := io.ReadAll(response.Body)
		require.NoError(t, err)
		require.NoError(t, response.Body.Close())
		require.Equal(t, "complete", string(body))
	}
	require.False(t, <-resumed)
	require.True(t, <-resumed, "a new TCP socket should resume the verified TLS session")
	transport.TLSClientConfig.RootCAs = x509.NewCertPool()
	transport.TLSClientConfig.ClientSessionCache = nil
	_, err = client.Get(server.URL)
	require.Error(t, err, "TLS verification must remain enforced")
}

func TestOpenAIHTTP2RetainsWarmConnectionWithoutExtendingOtherProfiles(t *testing.T) {
	server := httptest.NewUnstartedServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = io.WriteString(w, "complete")
	}))
	server.EnableHTTP2 = true
	server.StartTLS()
	t.Cleanup(server.Close)
	for _, tc := range []struct {
		name    string
		profile service.HTTPUpstreamProfile
		reused  bool
	}{
		{"openai_h2", service.HTTPUpstreamProfileOpenAI, true},
		{"native_default", service.HTTPUpstreamProfileDefault, false},
		{"long_stream", service.HTTPUpstreamProfileLongStream, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			cfg := &config.Config{Gateway: config.GatewayConfig{
				IdleConnTimeoutSeconds: 1,
				OpenAIHTTP2:            config.GatewayOpenAIHTTP2Config{Enabled: true, IdleConnTimeoutSeconds: 3},
			}}
			s, ok := NewHTTPUpstream(cfg).(*httpUpstreamService)
			require.True(t, ok)
			entry, err := s.getClientEntry("", 1, 1, tc.profile, false, false)
			require.NoError(t, err)
			transport, ok := entry.client.Transport.(*http.Transport)
			require.True(t, ok)
			t.Cleanup(transport.CloseIdleConnections)
			roots := x509.NewCertPool()
			roots.AddCert(server.Certificate())
			transport.TLSClientConfig.RootCAs = roots
			for attempt := 0; attempt < 2; attempt++ {
				var reused bool
				trace := &httptrace.ClientTrace{GotConn: func(info httptrace.GotConnInfo) { reused = info.Reused }}
				ctx := httptrace.WithClientTrace(context.Background(), trace)
				req, err := http.NewRequestWithContext(ctx, http.MethodGet, server.URL, nil)
				require.NoError(t, err)
				response, err := entry.client.Do(req)
				require.NoError(t, err)
				_, err = io.Copy(io.Discard, response.Body)
				require.NoError(t, err)
				require.NoError(t, response.Body.Close())
				if attempt == 0 {
					time.Sleep(1300 * time.Millisecond)
				} else {
					require.Equal(t, tc.reused, reused)
				}
			}
		})
	}
}
