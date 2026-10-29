package repository

import (
	"encoding/hex"
	"encoding/json"
	"io"
	"os"
	"sync"
	"sync/atomic"
	"time"
)

const maxProxyRouteStateBytes = 32 << 10

type proxyRouteSnapshot struct {
	Version int               `json:"version"`
	Routes  map[string]string `json:"routes"`
}

// One process-scoped watcher keeps disk I/O outside the request/scheduler path.
// Bad or missing snapshots retain the last verified route rather than flushing
// healthy connections on every request during a controller or storage outage.
type proxyRouteGenerations struct {
	path      string
	current   atomic.Pointer[proxyRouteSnapshot]
	refreshMu sync.Mutex
	stop      chan struct{}
	stopOnce  sync.Once
}

func newProxyRouteGenerations(path string) *proxyRouteGenerations {
	r := &proxyRouteGenerations{path: path, stop: make(chan struct{})}
	r.refresh()
	go func() {
		ticker := time.NewTicker(500 * time.Millisecond)
		defer ticker.Stop()
		for {
			select {
			case <-r.stop:
				return
			case <-ticker.C:
				r.refresh()
			}
		}
	}()
	return r
}

func (r *proxyRouteGenerations) close() {
	if r != nil {
		r.stopOnce.Do(func() { close(r.stop) })
	}
}

func (r *proxyRouteGenerations) refresh() {
	r.refreshMu.Lock()
	defer r.refreshMu.Unlock()
	f, err := os.Open(r.path)
	if err != nil {
		return
	}
	defer f.Close()
	raw, err := io.ReadAll(io.LimitReader(f, maxProxyRouteStateBytes+1))
	if err != nil || len(raw) > maxProxyRouteStateBytes {
		return
	}
	var snapshot proxyRouteSnapshot
	if json.Unmarshal(raw, &snapshot) != nil || snapshot.Version != 1 || len(snapshot.Routes) == 0 || len(snapshot.Routes) > 64 {
		return
	}
	canonical := make(map[string]string, len(snapshot.Routes))
	for proxyURL, generation := range snapshot.Routes {
		key, proxy, err := normalizeProxyURL(proxyURL)
		if err != nil || proxy == nil || len(generation) != 64 {
			return
		}
		if _, err := hex.DecodeString(generation); err != nil {
			return
		}
		if previous, exists := canonical[key]; exists && previous != generation {
			return
		}
		canonical[key] = generation
	}
	// Publishers may update one role at a time; a missing role is not a command
	// to invalidate a different provider's warm connections.
	if previous := r.current.Load(); previous != nil {
		for key, generation := range previous.Routes {
			if _, exists := canonical[key]; !exists {
				canonical[key] = generation
			}
		}
	}
	r.current.Store(&proxyRouteSnapshot{Version: 1, Routes: canonical})
}

func (s *httpUpstreamService) proxyRoutePoolKey(proxyKey, poolKey string) string {
	if s.proxyRoutes == nil {
		return poolKey
	}
	snapshot := s.proxyRoutes.current.Load()
	if snapshot == nil || snapshot.Routes[proxyKey] == "" {
		return poolKey
	}
	return poolKey + ":egress=" + snapshot.Routes[proxyKey]
}
