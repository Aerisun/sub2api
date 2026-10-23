package handler

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"sync"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/pkg/response"
	"github.com/gin-gonic/gin"
	"github.com/google/uuid"
)

// Only this request's IDs travel through SSE. Original bytes live briefly in
// memory after the model asks for them; no image payload enters a checkpoint.
type chatImageRequest struct {
	ID       string   `json:"id"`
	ImageIDs []string `json:"imageIDs"`
}
type chatImageDelivery struct {
	Inputs      map[string]string `json:"images"`
	Unavailable []string          `json:"unavailable"`
}
type chatImageMailbox struct {
	mu        sync.Mutex
	id        string
	images    []chatImage
	delivery  chan chatImageDelivery
	delivered bool
}

func (h *ChatHistoryHandler) ImageInputs(c *gin.Context) {
	uid, ok := chatUser(c)
	if !ok || !chatUUID(c) {
		return
	}
	var running bool
	if err := h.db.QueryRowContext(c.Request.Context(), `SELECT EXISTS(SELECT 1 FROM chat_generations WHERE user_id=$1 AND id=$2 AND status='running')`, uid, c.Param("id")).Scan(&running); err != nil {
		response.ErrorFrom(c, err)
		return
	}
	value, exists := h.imageRequests.Load(fmt.Sprintf("%d:%s", uid, c.Param("id")))
	if !running || !exists {
		response.NotFound(c, "图片读取请求已结束")
		return
	}
	c.Request.Body = http.MaxBytesReader(c.Writer, c.Request.Body, 96*1024*1024)
	var body struct {
		RequestID string `json:"requestID"`
		chatImageDelivery
	}
	if c.ShouldBindJSON(&body) != nil {
		response.BadRequest(c, "图片读取响应格式无效")
		return
	}
	mailbox := value.(*chatImageMailbox)
	if body.RequestID != mailbox.id {
		response.BadRequest(c, "图片读取请求不匹配")
		return
	}
	wanted := make(map[string]bool)
	for _, image := range mailbox.images {
		wanted[image.ClientID] = true
	}
	unavailable := make(map[string]bool)
	for _, id := range body.Unavailable {
		if !wanted[id] || unavailable[id] {
			response.BadRequest(c, "无效的图片读取响应")
			return
		}
		unavailable[id] = true
	}
	available := make([]chatImage, 0, len(mailbox.images))
	for _, image := range mailbox.images {
		if !unavailable[image.ClientID] {
			available = append(available, image)
		}
	}
	if err := validateChatImageInputs(available, body.Inputs); err != nil {
		response.BadRequest(c, err.Error())
		return
	}
	mailbox.mu.Lock()
	defer mailbox.mu.Unlock()
	if !mailbox.delivered {
		mailbox.delivered = true
		mailbox.delivery <- body.chatImageDelivery
	}
	response.Success(c, gin.H{"accepted": true})
}

func (h *ChatHistoryHandler) requestChatBrowserImages(ctx context.Context, uid int64, jobID string, images []chatImage, emit func(chatDelta) bool) (map[string]string, []chatImage, error) {
	inputs := make(map[string]string)
	preloaded, _ := ctx.Value(chatImageInputsKey{}).(map[string]string)
	wanted := make([]chatImage, 0, len(images))
	ids := make([]string, 0, len(images))
	for _, image := range images {
		if image.ClientID == "" {
			continue
		}
		if value := preloaded[image.ClientID]; value != "" {
			inputs[image.ClientID] = value
			continue
		}
		wanted = append(wanted, image)
		ids = append(ids, image.ClientID)
	}
	if len(wanted) == 0 {
		return inputs, nil, nil
	}
	mailbox := &chatImageMailbox{id: uuid.NewString(), images: wanted, delivery: make(chan chatImageDelivery, 1)}
	key := fmt.Sprintf("%d:%s", uid, jobID)
	if _, loaded := h.imageRequests.LoadOrStore(key, mailbox); loaded {
		return nil, nil, errors.New("图片读取请求重复")
	}
	defer h.imageRequests.Delete(key)
	defer emit(chatDelta{ImageRequest: &chatImageRequest{}})
	if !emit(chatDelta{ImageRequest: &chatImageRequest{ID: mailbox.id, ImageIDs: ids}}) {
		return nil, nil, ctx.Err()
	}
	timer := time.NewTimer(20 * time.Second)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return nil, nil, ctx.Err()
	case <-timer.C:
		return inputs, wanted, nil
	case delivery := <-mailbox.delivery:
		for id, value := range delivery.Inputs {
			inputs[id] = value
		}
		missing := make(map[string]bool)
		for _, id := range delivery.Unavailable {
			missing[id] = true
		}
		unavailable := make([]chatImage, 0, len(missing))
		for _, image := range wanted {
			if missing[image.ClientID] {
				unavailable = append(unavailable, image)
			}
		}
		return inputs, unavailable, nil
	}
}
