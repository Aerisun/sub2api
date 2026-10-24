package handler

import (
	"context"
	"io"
	"net"
	"net/http"
	"sync"
	"time"
)

func newChatHTTPClient() *http.Client {
	return &http.Client{
		Timeout:       20 * time.Minute,
		CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
		Transport: &http.Transport{
			// Bound connection setup independently of legitimate long responses.
			// Loopback calls never inherit an external HTTP proxy.
			DialContext:  (&net.Dialer{Timeout: 3 * time.Second, KeepAlive: 30 * time.Second}).DialContext,
			MaxIdleConns: 128, MaxIdleConnsPerHost: 64, IdleConnTimeout: 60 * time.Second,
			TLSHandshakeTimeout: 5 * time.Second, ExpectContinueTimeout: time.Second,
		},
	}
}

func doChatRequest(client *http.Client, request *http.Request) (*http.Response, error) {
	ctx, cancel := context.WithCancel(request.Context())
	owned := request.Clone(ctx)
	// A durable turn ID is used by our tools, but is not proof that every model
	// provider deduplicates generation POSTs. Disable net/http's implicit replay
	// of a request carrying Idempotency-Key after a reused connection breaks.
	owned.GetBody = nil
	response, err := client.Do(owned)
	if err != nil {
		cancel()
		return response, err
	}
	response.Body = &chatResponseBody{ReadCloser: response.Body, cancel: cancel}
	return response, nil
}

type chatResponseBody struct {
	io.ReadCloser
	cancel context.CancelFunc
	once   sync.Once
	err    error
}

func (b *chatResponseBody) Close() error {
	b.once.Do(func() {
		// An SSE terminal frame can arrive before HTTP EOF. Cancel that request
		// before an early body close so it cannot strand the next pooled caller.
		// Requests already at EOF have released the connection for normal reuse.
		b.cancel()
		b.err = b.ReadCloser.Close()
	})
	return b.err
}
