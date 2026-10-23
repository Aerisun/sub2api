package handler

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"strings"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/service"
	"github.com/google/uuid"
)

type chatImageInputsKey struct{}

const chatMaxRecognitionImages = 12

func chatImageKey(image chatImage) string {
	if image.ClientID != "" {
		return "browser:" + image.ClientID
	}
	return image.AssetID
}

// Validate transient bytes before saving any transcript. They are never passed
// to saveChatTx, chat_generations, tool events, disk or a background task queue.
func validateChatImageInputs(images []chatImage, inputs map[string]string) error {
	wanted := make(map[string]string)
	for _, image := range images {
		if image.ClientID != "" {
			wanted[image.ClientID] = image.MediaType
		}
	}
	if len(inputs) != len(wanted) || len(inputs) > chatMaxRecognitionImages || len(images) > chatMaxRecognitionImages {
		return errors.New("识图图片不完整，请重新上传")
	}
	for id, value := range inputs {
		media, ok := wanted[id]
		prefix := "data:" + media + ";base64,"
		if !ok || !strings.HasPrefix(value, prefix) {
			return errors.New("无效的识图图片")
		}
		raw, err := base64.StdEncoding.DecodeString(strings.TrimPrefix(value, prefix))
		if err != nil || len(raw) == 0 || len(raw) > 5*1024*1024 || http.DetectContentType(raw) != media {
			return errors.New("请选择 5 MB 以内的 PNG、JPG 或 WebP 图片")
		}
	}
	return nil
}

// The public catalogue also advertises images for models with a helper bridge.
// Only these verified native models receive image bytes in browser chat. New or
// text-only models use the standard tool until their native support is verified.
func chatModelHasNativeVision(model string) bool {
	name := strings.ToLower(strings.TrimSpace(model))
	if slash := strings.LastIndexByte(name, '/'); slash >= 0 {
		name = name[slash+1:]
	}
	switch name {
	case "gpt-5.6-luna", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-6-astra", "gpt-6-luna", "gpt-6-sol", "gpt-6.1-sol",
		"claude-opus-4-6-thinking", "claude-sonnet-4-6", "gemini-3.8-flash", "kimi-k3", "deepseek-v4.1-flash",
		"qwen3.8-flash", "qwen3.8-max", "glm-5.3-flash", "step-5-preview":
		return true
	}
	return false
}

type chatImageToolInput struct {
	Prompt     string      `json:"prompt,omitempty"`
	Images     []chatImage `json:"images"`
	BatchIndex int         `json:"batchIndex,omitempty"`
	BatchCount int         `json:"batchCount,omitempty"`
	ImageStart int         `json:"imageStart,omitempty"`
}

// A completed description can replace an old attachment even after switching
// from a native vision model. Never mistake another turn's result for this image.
func chatRecognizedImages(messages []chatMessage) map[string]bool {
	known := make(map[string]bool)
	for _, message := range messages {
		for _, tool := range message.Tools {
			if tool.Name != "image_recognition" || tool.Status != "completed" || strings.TrimSpace(tool.Output) == "" {
				continue
			}
			var input chatImageToolInput
			if json.Unmarshal(tool.Input, &input) == nil {
				for _, image := range input.Images {
					if image.AssetID != "" || image.ClientID != "" {
						known[chatImageKey(image)] = true
					}
				}
			}
		}
	}
	return known
}

// Use the exact authenticated REST tool used by external callers. Concurrency,
// GLM/NVIDIA routing, pricing and usage remain owned by that implementation.
// Do not retry a paid tool POST automatically, or reenter the legacy GLM bridge.
func (h *ChatHistoryHandler) recognizeChatImages(ctx context.Context, images []chatImage, urls []string, prompt, auth string, emit func(chatDelta) bool) (string, error) {
	if len(images) != 1 || len(urls) != 1 {
		return "", errors.New("每次识图调用只能识别一张图片")
	}
	runes := []rune(prompt)
	if len(runes) > 8000 {
		prompt = string(runes[:8000])
	}
	input := chatImageToolInput{Prompt: prompt, Images: images}
	if batch, ok := ctx.Value(chatImageBatchKey{}).(chatImageBatch); ok {
		input.BatchIndex, input.BatchCount, input.ImageStart = batch.Index, batch.Count, batch.Start
	}
	event := service.InternalToolEvent{Type: "tool_call", ID: "vision-" + uuid.NewString(), Name: "image_recognition", Status: "running", Input: mustChatJSON(input)}
	if !emit(chatDelta{Tool: &event}) {
		return "", ctx.Err()
	}
	fail := func(err error) (string, error) {
		failed := event
		failed.Status, failed.Error = "failed", err.Error()
		emit(chatDelta{Tool: &failed})
		return "", err
	}
	// Accept negotiation keeps mixed-version rolling deployments compatible.
	arguments := map[string]any{"image": urls[0]}
	// The vision model sees just this image. Give it the original position so a
	// multi-image question cannot make it invent observations of absent images.
	if input.BatchCount > 1 {
		prompt = fmt.Sprintf("本次只识别整组 %d 张图片中的第 %d 张。只描述当前图片中与问题有关的可见信息；跨图比较由对话模型汇总，不猜测其他图片。\n用户问题：%s", input.BatchCount, input.BatchIndex, prompt)
	}
	if strings.TrimSpace(prompt) != "" {
		arguments["prompt"] = string([]rune(prompt)[:min(len([]rune(prompt)), 8000)])
	}
	raw, err := json.Marshal(arguments)
	if err != nil {
		return fail(errors.New("识图请求格式无效"))
	}
	// The tool has separate bounded queue and inference budgets (90s each).
	ctx, cancel := context.WithTimeout(ctx, 190*time.Second)
	defer cancel()
	request, err := http.NewRequestWithContext(ctx, http.MethodPost, h.toolURL+"/v1/tools/image_recognition", bytes.NewReader(raw))
	if err != nil {
		return fail(errors.New("识图工具地址无效"))
	}
	request.Header.Set("Authorization", auth)
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("Accept", "text/event-stream")
	response, err := h.client.Do(request)
	if err != nil {
		return fail(errors.New("识图工具连接中断，请稍后重试"))
	}
	defer response.Body.Close()
	result, err := readChatRecognitionResponse(response, event, emit)
	if err != nil {
		return fail(err)
	}
	if response.StatusCode != http.StatusOK || result.Status != "completed" {
		message := result.Error.Message
		if strings.TrimSpace(message) == "" {
			message = fmt.Sprintf("识图工具调用失败（%d）", response.StatusCode)
		}
		return fail(errors.New(message))
	}
	if result.Name != "image_recognition" || result.Output.ImageCount != len(images) || strings.TrimSpace(result.Output.Text) == "" {
		return fail(errors.New("识图工具未返回完整的图片描述"))
	}
	completed := event
	completed.Status, completed.Output, completed.Cached = "completed", result.Output.Text, result.Cached
	completed.Metadata = result.Output.Metadata
	if !emit(chatDelta{Tool: &completed}) {
		return "", ctx.Err()
	}
	return result.Output.Text, nil
}
