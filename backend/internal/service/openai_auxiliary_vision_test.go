//go:build unit

package service

import (
	"bytes"
	"context"
	"encoding/base64"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/pkg/ctxkey"
	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
)

func TestVisionDescriptionIsContentWithoutRoutingInstructions(t *testing.T) {
	url := "data:image/png;base64,AAAA"
	payload := map[string]any{"messages": []any{map[string]any{"role": "user", "content": []any{
		map[string]any{"type": "text", "text": "分析这张框架图"},
		map[string]any{"type": "image_url", "image_url": map[string]any{"url": url}},
	}}}}
	rewriteAuxiliaryImagesAsText(payload, map[string]string{auxiliaryImageDigest(url): "Ego-Pi 框架包含视觉编码器和动作解码器。"})
	parts := payload["messages"].([]any)[0].(map[string]any)["content"].([]any)
	require.Equal(t, "分析这张框架图", parts[0].(map[string]any)["text"])
	require.Equal(t, "图片内容：\nEgo-Pi 框架包含视觉编码器和动作解码器。", parts[1].(map[string]any)["text"])
}

type auxiliaryVisionAccountRepo struct {
	AccountRepository
	account  *Account
	accounts map[int64]*Account
}

func (r auxiliaryVisionAccountRepo) GetByID(_ context.Context, id int64) (*Account, error) {
	if r.accounts != nil {
		return r.accounts[id], nil
	}
	return r.account, nil
}

func auxiliaryVisionFixture() (*Account, *Account, *OpenAIGatewayService, *httpUpstreamRecorder) {
	account := forceChatMessagesFallbackAccount()
	account.ID = 9
	account.Extra["nvidia_image_tool"] = "configured"
	account.Credentials["model_mapping"] = map[string]any{"Nemotron-3.5": "nvidia/nemotron-3.5-lightning-30b-a3b"}
	account.Extra["image_bridge_account_id"] = 5
	account.Extra["image_bridge_model"] = "kimi-k3"
	vision := &Account{ID: 5, Platform: PlatformOpenAI, Type: AccountTypeAPIKey,
		Status: StatusActive, Schedulable: true, Credentials: map[string]any{
			"base_url": "https://vision.example", "api_key": "vision-test-key",
			"model_mapping": map[string]any{"kimi-k3": "kimi-k3"}}}
	upstream := &httpUpstreamRecorder{resp: &http.Response{StatusCode: http.StatusOK,
		Header: http.Header{"Content-Type": []string{"application/json"}},
		Body:   io.NopCloser(strings.NewReader(`{"id":"vision-probe","object":"chat.completion","model":"kimi-k3","choices":[{"message":{"role":"assistant","content":"Red triangle, blue circle, number 4729."},"finish_reason":"stop"}],"usage":{"prompt_tokens":195,"completion_tokens":20,"total_tokens":215}}`))}}
	svc := &OpenAIGatewayService{accountRepo: auxiliaryVisionAccountRepo{account: vision},
		cfg: rawChatCompletionsTestConfig(), httpUpstream: upstream}
	return account, vision, svc, upstream
}

type visionSlotCounter struct {
	ConcurrencyCache
	acquires int
}

func (c *visionSlotCounter) AcquireAccountSlot(context.Context, int64, int, string) (bool, error) {
	c.acquires++
	return true, nil
}
func (c *visionSlotCounter) ReleaseAccountSlot(context.Context, int64, string) error { return nil }

func TestAuxiliaryVisionSharesMainAccountSlotAndPreservesEffort(t *testing.T) {
	for _, shared := range []bool{true, false} {
		account, vision, svc, upstream := auxiliaryVisionFixture()
		account.Extra["image_bridge_reasoning_effort"] = "low"
		vision.Credentials["model_mapping"] = map[string]any{"kimi-k3": "moonshotai/kimi-k3"}
		vision.Concurrency = 64
		if shared {
			account.ID = vision.ID
		}
		counter := &visionSlotCounter{}
		svc.concurrencyService = NewConcurrencyService(counter)
		_, usage, err := svc.describeAuxiliaryImageAttempt(context.Background(), account, vision.ID, "kimi-k3", "data:image/png;base64,AAAA")
		require.NoError(t, err)
		if shared {
			require.Zero(t, counter.acquires)
		} else {
			require.Equal(t, 1, counter.acquires)
		}
		require.Equal(t, "low", gjson.GetBytes(upstream.lastBody, "reasoning_effort").String())
		require.NotEqual(t, "disabled", gjson.GetBytes(upstream.lastBody, "thinking.type").String())
		require.Equal(t, "moonshotai/kimi-k3", gjson.GetBytes(upstream.lastBody, "model").String())
		require.Equal(t, "kimi-k3", usage.Result.BillingModel)
		require.Equal(t, "low", *usage.Result.ReasoningEffort)
	}
}

func TestAuxiliaryImageBridgeNamespacedVisionPreservesBillingAlias(t *testing.T) {
	for _, name := range []string{"kimi-k3", "deepseek-v4.1-flash", "qwen3.8-flash"} {
		t.Run(name, func(t *testing.T) {
			account, vision, svc, upstream := auxiliaryVisionFixture()
			mapped := "provider/" + name
			vision.Credentials["model_mapping"] = map[string]any{name: mapped}
			_, usage, err := svc.describeAuxiliaryImageAttempt(context.Background(), account, vision.ID, name, "data:image/png;base64,AAAA")
			require.NoError(t, err)
			require.Equal(t, mapped, gjson.GetBytes(upstream.lastBody, "model").String())
			require.Equal(t, name, usage.Result.Model)
			require.Equal(t, name, usage.Result.BillingModel)
			require.Equal(t, mapped, usage.Result.UpstreamModel)
			switch name {
			case "kimi-k3":
				require.Equal(t, "low", gjson.GetBytes(upstream.lastBody, "reasoning_effort").String())
				require.False(t, gjson.GetBytes(upstream.lastBody, "thinking").Exists())
			case "deepseek-v4.1-flash":
				require.Equal(t, "disabled", gjson.GetBytes(upstream.lastBody, "thinking.type").String())
				require.False(t, gjson.GetBytes(upstream.lastBody, "reasoning_effort").Exists())
			}
		})
	}
}

func TestNemotronImageToolUsesDeepSeekAndPreservesPublicControls(t *testing.T) {
	for _, protocol := range []string{"chat", "messages", "responses"} {
		t.Run(protocol, func(t *testing.T) {
			account, vision, svc, upstream := auxiliaryVisionFixture()
			account.ID = 66
			account.Extra["nvidia_image_tool"] = "deepseek"
			account.Extra["image_bridge_model"] = "deepseek-v4.1-flash"
			account.Credentials["model_mapping"] = map[string]any{"Nemotron-3.5": "nvidia/nemotron-3.5-lightning-30b-a3b"}
			vision.Credentials["model_mapping"] = map[string]any{"deepseek-v4.1-flash": "deepseek/deepseek-v4.1-flash-1017"}
			upstream.responses = []*http.Response{upstream.resp, {StatusCode: http.StatusOK,
				Header: http.Header{"Content-Type": []string{"application/json"}},
				Body:   io.NopCloser(strings.NewReader(`{"id":"nemotron-probe","model":"nvidia/nemotron-3.5-lightning-30b-a3b","choices":[{"message":{"role":"assistant","content":"4729"},"finish_reason":"stop"}],"usage":{"prompt_tokens":50,"completion_tokens":2,"total_tokens":52}}`))}}
			upstream.resp = nil
			body := []byte(`{"model":"Nemotron-3.5","max_tokens":128,"reasoning_effort":"max","messages":[{"role":"user","content":[{"type":"text","text":"Read the number"},{"type":"image_url","image_url":{"url":"data:image/png;base64,AAAA"}}]}]}`)
			if protocol == "messages" {
				body = []byte(`{"model":"Nemotron-3.5","max_tokens":128,"output_config":{"effort":"max"},"messages":[{"role":"user","content":[{"type":"text","text":"Read the number"},{"type":"image","source":{"type":"base64","media_type":"image/png","data":"AAAA"}}]}]}`)
			}
			if protocol == "responses" {
				body = []byte(`{"model":"Nemotron-3.5","max_output_tokens":128,"reasoning":{"effort":"max"},"input":[{"role":"user","content":[{"type":"input_text","text":"Read the number"},{"type":"input_image","image_url":"data:image/png;base64,AAAA"}]}]}`)
			}
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
			require.Len(t, upstream.requests, 2)
			require.Contains(t, string(upstream.lastBody), "4729")
			require.NotContains(t, string(upstream.lastBody), "image_url")
			require.Equal(t, "max", gjson.GetBytes(upstream.lastBody, "reasoning_effort").String())
			require.Len(t, result.AuxiliaryVisionUsage, 1)
			require.Equal(t, "deepseek-v4.1-flash", result.AuxiliaryVisionUsage[0].Result.BillingModel)
			require.Equal(t, "deepseek/deepseek-v4.1-flash-1017", result.AuxiliaryVisionUsage[0].Result.UpstreamModel)
			require.True(t, accountCodexModelSupportsImageInput(account, "Nemotron-3.5"))
			require.False(t, auxiliaryImageBridgeEnabled(account, "step-5-preview"))
			require.False(t, auxiliaryImageBridgeEnabled(account, "kimi-k3"))
		})
	}
}

func TestAuxiliaryImageBridgePreservesControlsAndCachesDuplicates(t *testing.T) {
	gin.SetMode(gin.TestMode)
	for _, model := range []string{"Nemotron-3.5"} {
		t.Run(model, func(t *testing.T) {
			account, _, svc, upstream := auxiliaryVisionFixture()
			c, _ := gin.CreateTestContext(httptest.NewRecorder())
			body := []byte(`{"model":"` + model + `","stream":true,"thinking":{"type":"enabled","budget_tokens":2048},"reasoning_effort":"low","temperature":0.4,"tools":[{"type":"function","function":{"name":"test"}}],"messages":[{"role":"user","content":[{"type":"text","text":"Read the image"},{"type":"image_url","image_url":{"url":"data:image/png;base64,AAAA"}},{"type":"image_url","image_url":{"url":"data:image/png;base64,AAAA"}},{"type":"file","file":{"file_id":"keep"}}]}]}`)
			got, err := svc.bridgeAuxiliaryImageInputs(context.Background(), c, account, body)
			require.NoError(t, err)
			require.Len(t, upstream.requests, 1)
			require.Equal(t, "vision.example", upstream.lastReq.URL.Host)
			require.Equal(t, "kimi-k3", gjson.GetBytes(upstream.lastBody, "model").String())
			for _, field := range []string{"model", "stream", "thinking", "reasoning_effort", "temperature", "tools"} {
				require.JSONEq(t, gjson.GetBytes(body, field).Raw, gjson.GetBytes(got, field).Raw)
			}
			require.Equal(t, "Read the image", gjson.GetBytes(got, "messages.0.content.0.text").String())
			require.Contains(t, gjson.GetBytes(got, "messages.0.content.1.text").String(), "4729")
			require.Contains(t, gjson.GetBytes(got, "messages.0.content.2.text").String(), "4729")
			require.Equal(t, "keep", gjson.GetBytes(got, "messages.0.content.3.file.file_id").String())
			replayed, err := svc.bridgeAuxiliaryImageInputs(context.Background(), c, account, body)
			require.NoError(t, err)
			require.Equal(t, got, replayed)
			require.Len(t, upstream.requests, 1)
			result := &OpenAIForwardResult{}
			attachAuxiliaryVisionUsage(c, result)
			require.Len(t, result.AuxiliaryVisionUsage, 1)
			require.Equal(t, int64(5), result.AuxiliaryVisionUsage[0].Account.ID)
			require.Equal(t, 195, result.AuxiliaryVisionUsage[0].Result.Usage.InputTokens)
			require.Equal(t, 20, result.AuxiliaryVisionUsage[0].Result.Usage.OutputTokens)
			id := result.AuxiliaryVisionUsage[0].Result.RequestID
			billingCtx := context.WithValue(context.Background(), ctxkey.ClientRequestID, "parent-request")
			require.Equal(t, id, resolveUsageBillingRequestID(billingCtx, id))
			require.True(t, accountCodexModelSupportsImageInput(account, model))
		})
	}
}

type auxiliaryVisionUsageLogRecorder struct {
	openAIRecordUsageLogRepoStub
	logs []*UsageLog
}

func (r *auxiliaryVisionUsageLogRecorder) Create(ctx context.Context, log *UsageLog) (bool, error) {
	r.logs = append(r.logs, log)
	return r.openAIRecordUsageLogRepoStub.Create(ctx, log)
}

func TestAuxiliaryImageBridgeBillingUsesSeparateModelAndRequestID(t *testing.T) {
	repo := &auxiliaryVisionUsageLogRecorder{openAIRecordUsageLogRepoStub: openAIRecordUsageLogRepoStub{inserted: true}}
	users := &openAIRecordUsageUserRepoStub{}
	svc := newOpenAIRecordUsageServiceForTest(repo, users, &openAIRecordUsageSubRepoStub{}, nil)
	cheap, expensive, _, _ := orderedResponseBillingModels(t, svc.billingService,
		UsageTokens{InputTokens: 20, OutputTokens: 10}, openAICheapFixtureModel, openAIPriceyFixtureModel)
	ctx := context.WithValue(context.Background(), ctxkey.ClientRequestID, "parent-request")
	err := svc.RecordUsage(ctx, &OpenAIRecordUsageInput{
		Result: &OpenAIForwardResult{RequestID: "primary", Model: expensive,
			Usage: OpenAIUsage{InputTokens: 20, OutputTokens: 10},
			AuxiliaryVisionUsage: []auxiliaryVisionUsage{{Account: &Account{ID: 5},
				Result: &OpenAIForwardResult{RequestID: "vision-bridge:auxiliary", Model: cheap,
					BillingModel: cheap, UpstreamModel: cheap, Usage: OpenAIUsage{InputTokens: 20, OutputTokens: 10}}}}},
		APIKey: &APIKey{ID: 10}, User: &User{ID: 20}, Account: &Account{ID: 9},
		ChannelUsageFields: ChannelUsageFields{OriginalModel: expensive, ChannelMappedModel: expensive,
			BillingModelSource: BillingModelSourceChannelMapped},
	})
	require.NoError(t, err)
	require.Len(t, repo.logs, 2)
	require.Equal(t, 2, users.deductCalls)
	require.Equal(t, "vision-bridge:auxiliary", repo.logs[0].RequestID)
	require.Equal(t, "client:parent-request", repo.logs[1].RequestID)
	require.Equal(t, cheap, repo.logs[0].Model)
	require.Equal(t, cheap, repo.logs[0].RequestedModel)
	require.Equal(t, int64(5), repo.logs[0].AccountID)
	require.Equal(t, int64(9), repo.logs[1].AccountID)
	require.Positive(t, repo.logs[0].ActualCost)
	require.Less(t, repo.logs[0].ActualCost, repo.logs[1].ActualCost)
}

func TestAuxiliaryImageBridgeFailureDoesNotSendImageToTextModel(t *testing.T) {
	account, _, svc, upstream := auxiliaryVisionFixture()
	upstream.resp.StatusCode = http.StatusBadGateway
	body := []byte(`{"model":"Nemotron-3.5","messages":[{"role":"user","content":[{"type":"image_url","image_url":{"url":"https://example.com/test.png"}}]}]}`)
	got, err := svc.bridgeAuxiliaryImageInputs(context.Background(), nil, account, body)
	wrong := &Account{ID: 1, Platform: PlatformOpenAI}
	require.Error(t, err)
	require.Nil(t, got)
	unchanged, err := svc.bridgeAuxiliaryImageInputs(context.Background(), nil, wrong, body)
	require.NoError(t, err)
	require.Equal(t, body, unchanged)
}

func TestForwardAsAnthropic_AuxiliaryImageBridge(t *testing.T) {
	gin.SetMode(gin.TestMode)
	account, _, svc, upstream := auxiliaryVisionFixture()
	account.Credentials["model_mapping"] = map[string]any{"Nemotron-3.5": "Nemotron-3.5"}
	upstream.responses = []*http.Response{upstream.resp, {StatusCode: http.StatusOK,
		Header: http.Header{"Content-Type": []string{"application/json"}}, Body: io.NopCloser(strings.NewReader(`{"id":"text-probe","object":"chat.completion","model":"Nemotron-3.5","choices":[{"message":{"role":"assistant","content":"4729"},"finish_reason":"stop"}],"usage":{"prompt_tokens":50,"completion_tokens":2,"total_tokens":52}}`))}}
	upstream.resp = nil
	body := []byte(`{"model":"Nemotron-3.5","max_tokens":128,"output_config":{"effort":"high"},"messages":[{"role":"user","content":[{"type":"text","text":"Read the number"},{"type":"image","source":{"type":"base64","media_type":"image/png","data":"AAAA"}}]}]}`)
	c, _ := gin.CreateTestContext(httptest.NewRecorder())
	c.Request = httptest.NewRequest(http.MethodPost, "/v1/messages", bytes.NewReader(body))
	result, err := svc.ForwardAsAnthropic(context.Background(), c, account, body, "", "")
	require.NoError(t, err)
	require.Len(t, upstream.requests, 2)
	require.False(t, gjson.GetBytes(upstream.lastBody, "thinking.type").Exists())
	require.Equal(t, "high", gjson.GetBytes(upstream.lastBody, "reasoning_effort").String())
	require.Contains(t, string(upstream.lastBody), "4729")
	require.NotContains(t, string(upstream.lastBody), "image_url")
	require.Len(t, result.AuxiliaryVisionUsage, 1)
	require.Equal(t, "kimi-k3", result.AuxiliaryVisionUsage[0].Result.Model)
	require.Equal(t, 50, result.Usage.InputTokens)
}

type nativeVisionBridgeForwarderStub struct {
	body       []byte
	accountID  int64
	models     []string
	failGemini bool
}

func (f *nativeVisionBridgeForwarderStub) ForwardAsChatCompletions(ctx context.Context, c *gin.Context, account *Account, body []byte, _ *ParsedRequest) (*ForwardResult, error) {
	f.body, f.accountID = body, account.ID
	model := gjson.GetBytes(body, "model").String()
	f.models = append(f.models, model)
	if f.failGemini && model == "gemini-3.8-flash" {
		return nil, context.DeadlineExceeded
	}
	c.Data(http.StatusOK, "application/json", []byte(`{"model":"claude-sonnet-4-6","choices":[{"message":{"role":"assistant","content":"Red triangle, blue circle, number 4729."},"finish_reason":"stop"}]}`))
	return &ForwardResult{Model: model, Usage: ClaudeUsage{InputTokens: 150, OutputTokens: 20, CacheReadInputTokens: 50}}, nil
}

func TestAuxiliaryImageBridgeGeminiLowThenSonnetFallback(t *testing.T) {
	for _, fail := range []bool{false, true} {
		t.Run(fmt.Sprintf("primary_failure_%v", fail), func(t *testing.T) {
			account, primary, svc, _ := auxiliaryVisionFixture()
			primary.ID, primary.Platform, primary.Type = 1, PlatformAntigravity, AccountTypeOAuth
			primary.Credentials["model_mapping"] = map[string]any{"gemini-3.8-flash": "gemini-3.8-flash-low"}
			fallback := *primary
			fallback.ID = 3
			fallback.Credentials = map[string]any{"model_mapping": map[string]any{"claude-sonnet-4-6": "claude-sonnet-4-6"}}
			account.Extra["image_bridge_account_id"] = 1
			account.Extra["image_bridge_model"] = "gemini-3.8-flash"
			account.Extra["image_bridge_fallback_account_id"] = 3
			account.Extra["image_bridge_fallback_model"] = "claude-sonnet-4-6"
			svc.accountRepo = auxiliaryVisionAccountRepo{accounts: map[int64]*Account{1: primary, 3: &fallback}}
			forwarder := &nativeVisionBridgeForwarderStub{failGemini: fail}
			svc.ConfigureNativeVisionBridge(forwarder, &GatewayService{})
			uri := "data:image/png;base64," + base64.StdEncoding.EncodeToString([]byte("\x89PNG\r\n\x1a\nfixture"))
			description, usage, err := svc.describeAuxiliaryImage(context.Background(), account, uri)
			require.NoError(t, err)
			require.Contains(t, description, "4729")
			if fail {
				require.Equal(t, []string{"gemini-3.8-flash", "claude-sonnet-4-6"}, forwarder.models)
				require.Equal(t, "none", gjson.GetBytes(forwarder.body, "reasoning_effort").String())
				require.Equal(t, int64(3), usage.Account.ID)
			} else {
				require.Equal(t, []string{"gemini-3.8-flash"}, forwarder.models)
				require.Equal(t, "low", gjson.GetBytes(forwarder.body, "reasoning_effort").String())
				require.Equal(t, "gemini-3.8-flash", usage.ClaudeResult.Model)
				require.Equal(t, int64(1), usage.Account.ID)
			}
		})
	}
}

func TestAuxiliaryImageBridgeNativeSonnetNoneDisablesThinking(t *testing.T) {
	account, vision, svc, _ := auxiliaryVisionFixture()
	vision.ID, vision.Platform, vision.Type = 3, PlatformAntigravity, AccountTypeOAuth
	vision.Credentials = newAntigravityCompatAccount(AccountTypeOAuth).Credentials
	account.Extra["image_bridge_account_id"] = 3
	account.Extra["image_bridge_model"] = "claude-sonnet-4-6"
	account.Extra["image_bridge_reasoning_effort"] = "none"
	upstream := &httpUpstreamRecorder{resp: antigravityCompatSuccessResponse()}
	native := newAntigravityCompatService(rawChatCompletionsTestConfig().Gateway, upstream)
	svc.ConfigureNativeVisionBridge(native, &GatewayService{})
	uri := "data:image/png;base64," + base64.StdEncoding.EncodeToString([]byte("\x89PNG\r\n\x1a\nfixture"))
	_, _, err := svc.describeAuxiliaryImage(context.Background(), account, uri)
	require.NoError(t, err)
	require.Equal(t, int64(0), gjson.GetBytes(upstream.lastBody, "request.generationConfig.thinkingConfig.thinkingBudget").Int())
	require.False(t, gjson.GetBytes(upstream.lastBody, "request.generationConfig.thinkingConfig.includeThoughts").Bool())
	require.Equal(t, int64(4096), gjson.GetBytes(upstream.lastBody, "request.generationConfig.maxOutputTokens").Int())
}

func TestAuxiliaryImageBridgeKeepaliveDoesNotBlockFailover(t *testing.T) {
	account, _, _, _ := auxiliaryVisionFixture()
	c, rec := newAntigravityCompatContext(http.MethodPost, "/v1/messages", nil)
	state := auxiliaryVisionRequestState(c)
	state.KeepaliveStarted = true
	stop := startOpenAISSEKeepalive(c, 5*time.Millisecond)
	time.Sleep(20 * time.Millisecond)
	stop()
	require.Contains(t, rec.Body.String(), ": keepalive\n\n")
	require.Equal(t, -1, OpenAICompactKeepaliveAdjustedWrittenSize(c))
	body := []byte(`{"model":"Nemotron-3.5","messages":[{"role":"user","content":[{"type":"image_url","image_url":{"url":"data:image/png;base64,AAAA"}}]}]}`)
	startAuxiliaryVisionKeepalive(c, account, body, true)()
	require.Equal(t, -1, OpenAICompactKeepaliveAdjustedWrittenSize(c))
}

func TestAuxiliaryImageBridgeSilentBodyReadIsBounded(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	inner := newContextBoundBlockingReadCloser(nil)
	inner.ctx = ctx
	reader := &auxiliaryVisionIdleReadCloser{ReadCloser: inner, cancel: cancel, idle: 10 * time.Millisecond}
	_, err := reader.Read(make([]byte, 1))
	require.ErrorIs(t, err, context.Canceled)
	require.NoError(t, reader.Close())
}

func TestAuxiliaryImageBridgeErrorsAfterKeepaliveTerminateInProtocol(t *testing.T) {
	for _, path := range []string{"/v1/messages", "/v1/chat/completions", "/v1/responses"} {
		t.Run(path, func(t *testing.T) {
			c, rec := newAntigravityCompatContext(http.MethodPost, path, nil)
			auxiliaryVisionRequestState(c).KeepaliveStarted = true
			stop := startOpenAISSEKeepalive(c, time.Hour)
			value, _ := c.Get(openAICompactSSEKeepaliveKey)
			require.True(t, value.(*openAICompactSSEKeepalive).beat())
			writeAnthropicError(c, http.StatusBadGateway, "api_error", "vision unavailable")
			stop()
			require.Equal(t, "text/event-stream", rec.Header().Get("Content-Type"))
			require.True(t, IsResponseCommitted(c))
			require.Contains(t, rec.Body.String(), "vision unavailable")
			if strings.Contains(path, "responses") {
				require.Contains(t, rec.Body.String(), "event: response.failed")
			} else {
				require.Contains(t, rec.Body.String(), "event: error\ndata:")
			}
		})
	}
}

func TestAuxiliaryImageBridgeUsesNativeClaudeAndBillsCacheTokensSeparately(t *testing.T) {
	account, vision, svc, _ := auxiliaryVisionFixture()
	account.Extra["image_bridge_account_id"] = 3
	account.Extra["image_bridge_model"] = "claude-sonnet-4-6"
	vision.ID, vision.Platform, vision.Type = 3, PlatformAntigravity, AccountTypeOAuth
	vision.Credentials["model_mapping"] = map[string]any{"claude-sonnet-4-6": "claude-sonnet-4-6"}
	forwarder := &nativeVisionBridgeForwarderStub{}
	repo := &auxiliaryVisionUsageLogRecorder{openAIRecordUsageLogRepoStub: openAIRecordUsageLogRepoStub{inserted: true}}
	users := &openAIRecordUsageUserRepoStub{}
	billing := newGatewayRecordUsageServiceForTest(repo, users, &openAIRecordUsageSubRepoStub{})
	svc.ConfigureNativeVisionBridge(forwarder, billing)
	// Minimal PNG signature suffices for the downloader's format sniffing.
	imageURL := "data:image/png;base64," + base64.StdEncoding.EncodeToString([]byte("\x89PNG\r\n\x1a\nimage fixture"))
	check := func(url string) auxiliaryVisionUsage {
		description, usage, err := svc.describeAuxiliaryImage(context.Background(), account, url)
		require.NoError(t, err)
		require.Contains(t, description, "4729")
		require.Equal(t, int64(3), forwarder.accountID)
		require.Equal(t, "claude-sonnet-4-6", gjson.GetBytes(forwarder.body, "model").String())
		require.Equal(t, imageURL, gjson.GetBytes(forwarder.body, "messages.0.content.1.image_url.url").String())
		require.Nil(t, usage.Result)
		require.Equal(t, 150, usage.ClaudeResult.Usage.InputTokens)
		require.Equal(t, 50, usage.ClaudeResult.Usage.CacheReadInputTokens)
		return usage
	}
	usage := check(imageURL)
	data, _ := base64.StdEncoding.DecodeString(strings.SplitN(imageURL, ",", 2)[1])
	svc.httpUpstream = &httpUpstreamRecorder{resp: &http.Response{StatusCode: 200, Header: make(http.Header), Body: io.NopCloser(bytes.NewReader(data))}}
	check("https://images.example.test/fixture.png")
	_, _, err := svc.describeAuxiliaryImage(context.Background(), account, "http://127.0.0.1/image.png")
	require.Error(t, err)
	svc.billingService = billing.billingService
	svc.usageLogRepo, svc.userRepo, svc.userSubRepo = repo, users, &openAIRecordUsageSubRepoStub{}
	svc.billingCacheService, svc.deferredService = billing.billingCacheService, &DeferredService{}
	svc.cfg.Default.RateMultiplier = 1.1
	ctx := context.WithValue(context.Background(), ctxkey.ClientRequestID, "same-client-id")
	err = svc.RecordUsage(ctx, &OpenAIRecordUsageInput{
		Result: &OpenAIForwardResult{RequestID: "primary", Model: openAICheapFixtureModel, Usage: OpenAIUsage{InputTokens: 20, OutputTokens: 10}, AuxiliaryVisionUsage: []auxiliaryVisionUsage{usage}},
		APIKey: &APIKey{ID: 10}, User: &User{ID: 20}, Account: account,
		ChannelUsageFields: ChannelUsageFields{OriginalModel: openAICheapFixtureModel},
	})
	require.NoError(t, err)
	require.Len(t, repo.logs, 2)
	require.Equal(t, 2, users.deductCalls)
	require.Equal(t, "claude-sonnet-4-6", repo.logs[0].RequestedModel)
	require.Equal(t, 150, repo.logs[0].InputTokens)
	require.Equal(t, 50, repo.logs[0].CacheReadTokens)
	require.Equal(t, int64(3), repo.logs[0].AccountID)
	require.Equal(t, usage.ClaudeResult.RequestID, repo.logs[0].RequestID)
	require.Equal(t, "client:same-client-id", repo.logs[1].RequestID)
	require.Positive(t, repo.logs[0].ActualCost)
}

func TestAuxiliaryImageBridgeAcceptsPaddedAndUnpaddedBase64Images(t *testing.T) {
	for _, content := range []string{"a", "ab", "abc"} {
		encoded := base64.StdEncoding.EncodeToString([]byte(content))
		for _, input := range []string{encoded, strings.TrimRight(encoded, "="), "data:image/png;base64," + encoded} {
			normalized := normalizeOpenAIImageBase64(input)
			decoded, err := base64.StdEncoding.DecodeString(normalized)
			require.NoError(t, err)
			require.Equal(t, content, string(decoded))
		}
	}
}

func TestAuxiliaryImageBridgeFallbackUsesNativeChatForConfiguredTextModel(t *testing.T) {
	account, _, _, _ := auxiliaryVisionFixture()
	account.Extra["openai_responses_mode"] = "auto"
	account.Extra["openai_responses_supported"] = true
	require.False(t, shouldForwardOpenAIResponsesViaRawChatCompletions(account))
	for _, model := range []string{"Nemotron-3.5"} {
		require.True(t, shouldForwardOpenAIResponsesViaRawChatCompletionsForBody(account, []byte(`{"model":"`+model+`"}`)))
	}
	require.False(t, shouldForwardOpenAIResponsesViaRawChatCompletionsForBody(account, []byte(`{"model":"gpt-5.1"}`)))
}
