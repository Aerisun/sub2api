package service

import (
	"context"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"os"
	"time"
)

type realtimeTariff struct {
	textIn, textCached, textOut, audioIn, audioCached, audioOut, imageIn, imageCached float64
}

// USD per million tokens, from the official OpenAI model prices.
var realtimeTariffs = map[string]realtimeTariff{
	"gpt-realtime-2.1":      {4, .4, 24, 32, .4, 64, 5, .5},
	"gpt-realtime-2.1-mini": {.6, .06, 2.4, 10, .3, 20, .8, .08},
	"gpt-realtime-2":        {4, .4, 24, 32, .4, 64, 5, .5},
	"gpt-realtime-1.5":      {4, .4, 16, 32, .4, 64, 5, .5},
}

func IsStandardOpenAIRealtimeModel(model string) bool {
	_, ok := realtimeTariffs[model]
	return ok
}

type realtimeModalTokens struct {
	Text  int `json:"text_tokens"`
	Audio int `json:"audio_tokens"`
	Image int `json:"image_tokens"`
}

func (m realtimeModalTokens) total() int { return m.Text + m.Audio + m.Image }
func (m realtimeModalTokens) valid() bool {
	return m.Text >= 0 && m.Text <= 1_000_000 && m.Audio >= 0 && m.Audio <= 1_000_000 &&
		m.Image >= 0 && m.Image <= 1_000_000
}

// RealtimeTokenUsage is taken only from the upstream response.done event.
type RealtimeTokenUsage struct {
	InputTokens  int `json:"input_tokens"`
	OutputTokens int `json:"output_tokens"`
	Input        struct {
		realtimeModalTokens
		CachedTokens int                 `json:"cached_tokens"`
		Cached       realtimeModalTokens `json:"cached_tokens_details"`
	} `json:"input_token_details"`
	Output realtimeModalTokens `json:"output_token_details"`
}

func (u *RealtimeTokenUsage) valid() bool {
	return u != nil && u.Input.valid() && u.Input.Cached.valid() && u.Output.valid() &&
		u.Input.total() == u.InputTokens && u.Output.total() == u.OutputTokens && u.Output.Image == 0 &&
		u.Input.Cached.total() == u.Input.CachedTokens &&
		u.Input.Cached.Text <= u.Input.Text && u.Input.Cached.Audio <= u.Input.Audio && u.Input.Cached.Image <= u.Input.Image
}

func (u *RealtimeTokenUsage) cost(model string, multiplier float64) (*CostBreakdown, error) {
	p, ok := realtimeTariffs[model]
	if !ok || !u.valid() {
		return nil, errors.New("invalid realtime token pricing or upstream usage")
	}
	b := &CostBreakdown{
		InputCost: (float64(u.Input.Text-u.Input.Cached.Text)*p.textIn +
			float64(u.Input.Audio-u.Input.Cached.Audio)*p.audioIn) / 1e6,
		ImageInputCost: float64(u.Input.Image-u.Input.Cached.Image) * p.imageIn / 1e6,
		OutputCost:     (float64(u.Output.Text)*p.textOut + float64(u.Output.Audio)*p.audioOut) / 1e6,
		CacheReadCost: (float64(u.Input.Cached.Text)*p.textCached +
			float64(u.Input.Cached.Audio)*p.audioCached + float64(u.Input.Cached.Image)*p.imageCached) / 1e6,
		BillingMode: string(BillingModeToken),
	}
	b.TotalCost = b.InputCost + b.ImageInputCost + b.OutputCost + b.CacheReadCost
	b.ActualCost = b.TotalCost * multiplier
	return b, nil
}

func ParseOpenAIRealtimeUsage(event []byte, model string) (*OpenAIForwardResult, *RealtimeTokenUsage, error) {
	var value struct {
		Type     string `json:"type"`
		Response struct {
			ID     string              `json:"id"`
			Status string              `json:"status"`
			Usage  *RealtimeTokenUsage `json:"usage"`
		} `json:"response"`
	}
	if err := json.Unmarshal(event, &value); err != nil || value.Type != "response.done" || !IsStandardOpenAIRealtimeModel(model) {
		return nil, nil, errors.New("invalid realtime terminal event")
	}
	r := value.Response
	if r.Usage == nil && (r.Status == "failed" || r.Status == "cancelled") {
		return nil, nil, nil
	}
	if !r.Usage.valid() || r.ID == "" || len(r.ID) > 256 {
		return nil, nil, errors.New("realtime terminal event has no verifiable usage")
	}
	if r.Usage.InputTokens == 0 && r.Usage.OutputTokens == 0 {
		return nil, nil, nil
	}
	digest := sha256.Sum256([]byte(r.ID))
	return &OpenAIForwardResult{
		RequestID: fmt.Sprintf("openai-realtime:%x", digest[:24]),
		Model:     model, UpstreamModel: model, Stream: true,
		Usage: OpenAIUsage{InputTokens: r.Usage.InputTokens, OutputTokens: r.Usage.OutputTokens,
			CacheReadInputTokens: r.Usage.Input.CachedTokens, ImageInputTokens: r.Usage.Input.Image,
			ImageCacheReadTokens: r.Usage.Input.Cached.Image},
	}, r.Usage, nil
}

// OpenStandardRealtimeBridge sends credentials only to the owner-only local
// Unix socket. The worker connects to the fixed, verified ChatGPT WebRTC URL.
func (s *OpenAIGatewayService) OpenStandardRealtimeBridge(ctx context.Context, account *Account, model string) (net.Conn, error) {
	if account == nil || !account.IsOpenAIOAuth() || !IsStandardOpenAIRealtimeModel(model) || account.GetMappedModel(model) != model {
		return nil, ErrLiveUnavailable
	}
	token, _, err := s.GetAccessToken(ctx, account)
	if err != nil {
		return nil, err
	}
	headers, err := s.buildOpenAIAuthenticationHeaders(ctx, account, token)
	if err != nil {
		return nil, err
	}
	if err := resolveAndSetOpenAIChatGPTAccountHeaders(ctx, s.accountRepo, headers, account); err != nil {
		return nil, err
	}
	ensureCodexIdentityHeaders(headers)
	enforceCodexIdentityHeaders(headers)
	socket := os.Getenv("SUB2API_REALTIME_BRIDGE_SOCKET")
	if socket == "" {
		socket = "var/run/openai-realtime/bridge.sock"
	}
	conn, err := (&net.Dialer{Timeout: time.Second}).DialContext(ctx, "unix", socket)
	if err != nil {
		return nil, err
	}
	_ = conn.SetWriteDeadline(time.Now().Add(time.Second))
	err = json.NewEncoder(conn).Encode(map[string]any{
		"model": model, "proxy": resolveAccountProxyURL(account), "headers": headers,
	})
	if err != nil {
		_ = conn.Close()
		return nil, err
	}
	_ = conn.SetWriteDeadline(time.Time{})
	return conn, nil
}
