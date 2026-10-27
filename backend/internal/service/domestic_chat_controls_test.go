//go:build unit

package service

import (
	"context"
	"fmt"
	"io"
	"net/http"
	"strings"
	"testing"

	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
)

func TestDomesticReasoningScaleAndSwitch(t *testing.T) {
	for _, model := range []string{"deepseek/deepseek-v4.1-flash", "deepseek/deepseek-v4.1-flash-1017", "moonshotai/kimi-k3", "moonshotai/kimi-k3-free"} {
		for _, tc := range []struct{ effort, want string }{{"auto", "low"}, {"low", "low"}, {"medium", "high"}, {"high", "high"}, {"xhigh", "max"}, {"max", "max"}} {
			t.Run(model+"/"+tc.effort, func(t *testing.T) {
				original := []byte(fmt.Sprintf(`{"model":%q,"reasoning_effort":%q}`, model, tc.effort))
				out, err := prepareDomesticChatControls(original, original, model, "chat")
				require.NoError(t, err)
				want := tc.want
				if strings.Contains(model, "deepseek") && tc.effort == "xhigh" {
					want = "high"
				}
				require.Equal(t, want, gjson.GetBytes(out, "reasoning_effort").String())
			})
		}
		original := []byte(`{"thinking":{"type":"disabled"},"reasoning_effort":"max"}`)
		out, err := prepareDomesticChatControls(original, original, model, "chat")
		require.NoError(t, err)
		if strings.Contains(model, "deepseek") {
			require.Equal(t, "disabled", gjson.GetBytes(out, "thinking.type").String())
			require.False(t, gjson.GetBytes(out, "reasoning_effort").Exists())
		} else {
			require.Equal(t, "low", gjson.GetBytes(out, "reasoning_effort").String())
		}
	}
	other := []byte(`{ "model":"unhandled-text-model", "reasoning_effort":"xhigh" }`)
	out, err := prepareDomesticChatControls(other, other, "unhandled-text-model", "chat")
	require.NoError(t, err)
	require.Equal(t, string(other), string(out))
}

func TestDomesticProtocolsPreserveThinkingCacheAndAffinity(t *testing.T) {
	for _, protocol := range []string{"chat", "messages", "responses"} {
		for _, model := range []string{"deepseek-v4.1-flash", "kimi-k3", "kimi-k3-free", "nemotron-3.5-lightning-30b-a3b", "step-5-preview"} {
			t.Run(model+"/"+protocol, func(t *testing.T) {
				input := `"messages":[{"role":"user","content":"hello"}]`
				effort := `"reasoning_effort":"max"`
				if protocol == "responses" {
					input = `"input":"hello"`
					effort = `"reasoning":{"effort":"max"}`
				}
				if protocol == "messages" {
					effort = `"output_config":{"effort":"max"}`
				}
				body := []byte(fmt.Sprintf(`{"model":%q,"stream":false,"max_tokens":64,%s,%s,"thinking":{"type":"enabled"},"metadata":{"user_id":"stable-conversation"},"prompt_cache_key":"session-one","prompt_cache_options":{"mode":"implicit","ttl":"1h"}}`, model, input, effort))
				upstream := &httpUpstreamRecorder{resp: &http.Response{StatusCode: 200, Header: http.Header{"Content-Type": []string{"application/json"}}, Body: io.NopCloser(strings.NewReader(`{"id":"cc","model":"upstream","choices":[{"message":{"role":"assistant","reasoning_content":"reason","content":"OK"},"finish_reason":"stop"}],"usage":{"prompt_tokens":1300,"completion_tokens":8,"prompt_tokens_details":{"cached_tokens":1024,"cache_write_tokens":128}}}`))}}
				svc := &OpenAIGatewayService{cfg: rawChatCompletionsTestConfig(), httpUpstream: upstream}
				account := forceChatMessagesFallbackAccount()
				c := adaptiveProtocolTestContext("/v1/"+protocol, body)
				var result *OpenAIForwardResult
				var err error
				switch protocol {
				case "chat":
					result, err = svc.ForwardAsChatCompletions(context.Background(), c, account, body, "", "")
				case "messages":
					result, err = svc.ForwardAsAnthropic(context.Background(), c, account, body, "", "")
				default:
					result, err = svc.Forward(context.Background(), c, account, body)
				}
				require.NoError(t, err)
				require.Equal(t, "max", gjson.GetBytes(upstream.lastBody, "reasoning_effort").String())
				require.Equal(t, "max", optionalStringValue(result.ReasoningEffort))
				require.Equal(t, "stable-conversation", gjson.GetBytes(upstream.lastBody, "metadata.user_id").String())
				require.Equal(t, "session-one", gjson.GetBytes(upstream.lastBody, "prompt_cache_key").String())
				require.Equal(t, "1h", gjson.GetBytes(upstream.lastBody, "prompt_cache_options.ttl").String())
				require.Equal(t, 1024, result.Usage.CacheReadInputTokens)
				require.Equal(t, 128, result.Usage.CacheCreationInputTokens)
			})
		}
	}
}

func TestDomesticMessagesTTLAndToolThinkingHistory(t *testing.T) {
	chat := []byte(`{"model":"kimi-k3","messages":[],"reasoning_effort":"medium"}`)
	original := []byte(`{"cache_control":{"type":"ephemeral","ttl":"1h"},"output_config":{"effort":"max"}}`)
	out, err := prepareDomesticChatControls(chat, original, "kimi-k3", "messages")
	require.NoError(t, err)
	require.Equal(t, "1h", gjson.GetBytes(out, "prompt_cache_options.ttl").String())
	require.Equal(t, "implicit", gjson.GetBytes(out, "prompt_cache_options.mode").String())
	require.Equal(t, "max", gjson.GetBytes(out, "reasoning_effort").String())
}

func TestDomesticNvidiaTextOnlyImageReturnsClientErrorWithoutCallingUpstream(t *testing.T) {
	for _, protocol := range []string{"chat", "messages", "responses"} {
		t.Run(protocol, func(t *testing.T) {
			body := []byte(`{"model":"nvidia/nemotron-3.5-lightning-30b-a3b","max_tokens":128,"messages":[{"role":"user","content":[{"type":"image_url","image_url":{"url":"data:image/png;base64,AA=="}}]}]}`)
			if protocol == "messages" {
				body = []byte(`{"model":"nvidia/nemotron-3.5-lightning-30b-a3b","max_tokens":128,"messages":[{"role":"user","content":[{"type":"image","source":{"type":"base64","media_type":"image/png","data":"AA=="}}]}]}`)
			}
			if protocol == "responses" {
				body = []byte(`{"model":"nvidia/nemotron-3.5-lightning-30b-a3b","max_output_tokens":128,"input":[{"role":"user","content":[{"type":"input_image","image_url":"data:image/png;base64,AA=="}]}]}`)
			}
			upstream := &httpUpstreamRecorder{}
			svc := &OpenAIGatewayService{cfg: rawChatCompletionsTestConfig(), httpUpstream: upstream}
			c := adaptiveProtocolTestContext("/v1/"+protocol, body)
			account := forceChatMessagesFallbackAccount()
			var err error
			switch protocol {
			case "chat":
				_, err = svc.ForwardAsChatCompletions(context.Background(), c, account, body, "", "")
			case "messages":
				_, err = svc.ForwardAsAnthropic(context.Background(), c, account, body, "", "")
			default:
				_, err = svc.Forward(context.Background(), c, account, body)
			}
			require.ErrorContains(t, err, "does not support image input")
			require.Equal(t, http.StatusBadRequest, c.Writer.Status())
			require.Empty(t, upstream.lastBody)
		})
	}
}
