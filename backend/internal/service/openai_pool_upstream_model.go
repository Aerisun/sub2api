package service

import (
	"net"
	"net/http"
	"net/url"
)

// Authenticated local credential pools own their key/model cooldowns. Applying
// another breaker to their aggregate account turns a few failed members into
// an outage for every healthy member. Opt-in never enables request replay.
func poolManagesTransientErrors(account *Account) bool {
	if account == nil || account.Platform != PlatformOpenAI || !account.IsPoolMode() {
		return false
	}
	managed, _ := account.Credentials["pool_manages_transient_errors"].(bool)
	if !managed {
		return false
	}
	base, err := url.Parse(account.GetBaseURL())
	if err != nil || base.Scheme != "http" {
		return false
	}
	ip := net.ParseIP(base.Hostname())
	return ip != nil && ip.IsLoopback()
}

// Only configured credential pools may report a different physical model, and
// only one explicitly provisioned on the account. Billing keeps its public ID.
func resolvePoolUpstreamModel(account *Account, headers http.Header, fallback string) string {
	if account == nil || !account.IsPoolMode() {
		return fallback
	}
	actual := headers.Get("X-Sub2api-Upstream-Model")
	for _, mapped := range account.GetModelMapping() {
		if actual != "" && actual == mapped {
			return actual
		}
	}
	return fallback
}
