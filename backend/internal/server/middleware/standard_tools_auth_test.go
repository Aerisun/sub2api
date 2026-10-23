package middleware

import (
	"github.com/stretchr/testify/require"
	"net/http"
	"testing"
)

func TestStandardToolsOnlyMetadataSkipsBilling(t *testing.T) {
	for _, path := range []string{"/v1/models", "/models"} {
		require.True(t, isReadOnlyModelCatalogue(http.MethodGet, path))
		require.False(t, isReadOnlyModelCatalogue(http.MethodPost, path))
	}
	for _, path := range []string{"/v1/images/generations", "/v1/images/generations/async", "/v1/chat/completions", "/v1/alpha/search", "/v1/models/arbitrary"} {
		require.False(t, isReadOnlyModelCatalogue(http.MethodGet, path))
		require.False(t, isReadOnlyModelCatalogue(http.MethodPost, path))
	}
}
