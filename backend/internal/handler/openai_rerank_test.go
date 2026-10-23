//go:build unit

package handler

import (
	"strings"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/service"
	"github.com/stretchr/testify/require"
)

func TestRerankRejectsInvalidPairsBeforeAdmission(t *testing.T) {
	for _, body := range []string{
		`{"query":"","documents":["文档"]}`,
		`{"query":7,"documents":["文档"]}`,
		`{"query":"查询","documents":[]}`,
		`{"query":"查询","documents":[{"text":"文档"}]}`,
		`{"query":"查询","documents":[""]}`,
		`{"query":"查询","documents":["文档"],"top_n":0}`,
		`{"query":"查询","documents":["文档"],"top_n":1.5}`,
		`{"query":"查询","documents":["文档"],"top_n":2}`,
		`{"query":"查询","documents":[` + strings.Repeat(`"文档",`, 100) + `"文档"]}`,
	} {
		require.NotEmpty(t, validateRerankBody([]byte(body)), body)
	}
	require.Empty(t, validateRerankBody([]byte(`{"query":"查询","documents":["文档一","文档二"],"top_n":1}`)))
}

func TestRerankEndpointAliasesRecordNativeEndpoint(t *testing.T) {
	for _, path := range []string{"/v1/rerank", "/rerank", "/v1/reranking", "/reranking"} {
		require.Equal(t, EndpointRerank, NormalizeInboundEndpoint(path))
		require.Equal(t, EndpointRerank, DeriveUpstreamEndpoint(EndpointRerank, path, service.PlatformOpenAI))
	}
}
