package service

import (
	"net/http"
	"strings"
)

func invalidateAntigravityRejectedRoute(p antigravityRetryLoopParams, status int, body []byte) bool {
	if status != http.StatusBadRequest || strings.TrimSpace(p.proxyURL) == "" ||
		!strings.EqualFold(strings.TrimSpace(extractAntigravityErrorMessage(body)), "User location is not supported for the API use.") {
		return false
	}
	invalidator, ok := p.httpUpstream.(HTTPUpstreamPoolInvalidator)
	if !ok {
		return false
	}
	invalidator.InvalidateConnectionPool(p.proxyURL, p.account.ID)
	return true
}
