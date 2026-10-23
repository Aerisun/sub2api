package handler

import (
	"context"
	"encoding/base64"
	"errors"
	"fmt"
	"strings"
	"sync"
)

// References are resolved only against user attachments in this conversation's
// earlier turns, after append has merged the owned transcript under its lock.
func chatSelectedHistoryImages(messages []chatMessage) ([]chatImage, error) {
	if len(messages) < 2 {
		return nil, nil
	}
	current := messages[len(messages)-2]
	if len(current.Images)+len(current.RecognitionImages) > chatMaxRecognitionImages {
		return nil, errors.New("本轮最多选择 12 张图片")
	}
	known := make(map[string]chatImage)
	for _, message := range messages[:len(messages)-2] {
		if message.Role == "user" {
			for _, image := range message.Images {
				known[chatImageKey(image)] = image
			}
		}
	}
	seen := make(map[string]bool)
	for _, image := range current.Images {
		key := chatImageKey(image)
		if seen[key] {
			return nil, errors.New("请勿重复选择同一张图片")
		}
		seen[key] = true
	}
	selected := make([]chatImage, 0, len(current.RecognitionImages))
	for _, key := range current.RecognitionImages {
		image, exists := known[key]
		if !exists || seen[key] {
			return nil, errors.New("历史图片选择已失效，请重新选择")
		}
		seen[key] = true
		selected = append(selected, image)
	}
	return selected, nil
}

func (h *ChatHistoryHandler) chatImageURL(ctx context.Context, uid int64, image chatImage, loaded map[string]string) (string, error) {
	key := chatImageKey(image)
	if value := loaded[key]; value != "" {
		return value, nil
	}
	if image.ClientID != "" {
		inputs, _ := ctx.Value(chatImageInputsKey{}).(map[string]string)
		if value := inputs[image.ClientID]; value != "" {
			loaded[key] = value
			return value, nil
		}
		return "", errors.New("图片仅保存在原浏览器，请重新上传后重试")
	}
	var media string
	var raw []byte
	if err := h.db.QueryRowContext(ctx, `SELECT media_type,content FROM chat_images WHERE user_id=$1 AND id=$2`, uid, image.AssetID).Scan(&media, &raw); err != nil {
		return "", errors.New("图片读取失败")
	}
	value := "data:" + media + ";base64," + base64.StdEncoding.EncodeToString(raw)
	loaded[key] = value
	return value, nil
}

type chatImageBatchKey struct{}
type chatImageBatch struct{ Index, Count, Start int }

// Every image has an independent, concurrent single-image tool call. Its live
// events and charge stay separate; results retain attachment order, not finish
// order. Shared tool and user concurrency limits still apply at the gateway.
func (h *ChatHistoryHandler) recognizeChatImageBatches(ctx context.Context, images []chatImage, urls []string, prompt, auth string, emit func(chatDelta) bool) (string, error) {
	if len(images) == 0 || len(images) > chatMaxRecognitionImages || len(images) != len(urls) {
		return "", errors.New("本轮识图支持 1–12 张图片")
	}
	if len(images) == 1 {
		return h.recognizeChatImages(ctx, images, urls, prompt, auth, emit)
	}
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	count := len(images)
	results := make([]string, count)
	var firstErr error
	var once sync.Once
	var group sync.WaitGroup
	for batch := 0; batch < count; batch++ {
		group.Add(1)
		go func(batch int) {
			defer group.Done()
			batchCtx := context.WithValue(ctx, chatImageBatchKey{}, chatImageBatch{batch + 1, count, batch + 1})
			result, err := h.recognizeChatImages(batchCtx, images[batch:batch+1], urls[batch:batch+1], prompt, auth, emit)
			if err != nil {
				once.Do(func() { firstErr = err; cancel() })
				return
			}
			results[batch] = fmt.Sprintf("图片 %d：\n%s", batch+1, result)
		}(batch)
	}
	group.Wait()
	if firstErr != nil {
		return "", firstErr
	}
	return strings.Join(results, "\n\n"), nil
}
