package service

import (
	"context"

	"github.com/Wei-Shaw/sub2api/internal/pkg/ctxkey"
	openaiwsv2 "github.com/Wei-Shaw/sub2api/internal/service/openai_ws_v2"
	coderws "github.com/coder/websocket"
	"github.com/tidwall/gjson"
	"github.com/tidwall/sjson"
)

// codexUpstreamServiceTier applies only the group's transport policy. The
// request retained by forwarding and billing must keep its post-policy tier.
func codexUpstreamServiceTier(ctx context.Context, account *Account, model, tier string) string {
	if !codexFastUsesStandardUpstream(ctx, account) {
		return tier
	}
	switch normalizedOpenAIServiceTierValue(tier) {
	case OpenAIFastTierPriority:
		return "default"
	case OpenAIFastTierUltrafast:
		if isOpenAIGPT62Model(model) {
			return "default"
		}
	}
	return tier
}

func codexFastUsesStandardUpstream(ctx context.Context, account *Account) bool {
	if ctx == nil || !account.IsOpenAIOAuthLike() {
		return false
	}
	group, _ := ctx.Value(ctxkey.Group).(*Group)
	return IsGroupContextValid(group) && groupSupportsOpenAIFast(group.Platform) && group.OpenAIFastUpstreamStandard
}

func rewriteCodexFastUpstreamBody(ctx context.Context, account *Account, body []byte) ([]byte, error) {
	if !codexFastUsesStandardUpstream(ctx, account) {
		return body, nil
	}
	fields := gjson.GetManyBytes(body, "service_tier", "type", "model")
	if fields[0].Type != gjson.String || (fields[1].Exists() && fields[1].String() != "response.create") {
		return body, nil
	}
	tier := fields[0].String()
	upstreamTier := codexUpstreamServiceTier(ctx, account, fields[2].String(), tier)
	if upstreamTier == tier {
		return body, nil
	}
	// SetBytes allocates a separate body, preserving the billable request.
	return sjson.SetBytes(body, "service_tier", upstreamTier)
}

// The passthrough relay captures billing metadata before this transport
// wrapper rewrites each response.create, including the first turn.
type openAICodexServiceTierFrameConn struct {
	openaiwsv2.FrameConn
	requestCtx context.Context
	account    *Account
}

func (c *openAICodexServiceTierFrameConn) WriteFrame(ctx context.Context, msgType coderws.MessageType, payload []byte) error {
	if msgType == coderws.MessageText || msgType == coderws.MessageBinary {
		updated, err := rewriteCodexFastUpstreamBody(c.requestCtx, c.account, payload)
		if err != nil {
			return err
		}
		payload = updated
	}
	return c.FrameConn.WriteFrame(ctx, msgType, payload)
}
