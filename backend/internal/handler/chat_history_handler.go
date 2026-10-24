package handler

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/Wei-Shaw/sub2api/internal/service"
	"net/http"
	"strings"
	"sync"
	"unicode/utf8"

	"github.com/Wei-Shaw/sub2api/internal/pkg/response"
	"github.com/Wei-Shaw/sub2api/internal/server/middleware"
	"github.com/gin-gonic/gin"
	"github.com/google/uuid"
)

type ChatHistoryHandler struct {
	liveTools     sync.Map
	imageRequests sync.Map
	db            *sql.DB
	baseURL       string
	toolURL       string
	client        *http.Client
	mu            sync.Mutex
	tasks         map[string]context.CancelFunc
}

func NewChatHistoryHandler(db *sql.DB, cfg *config.Config) *ChatHistoryHandler {
	h := &ChatHistoryHandler{db: db, baseURL: fmt.Sprintf("http://127.0.0.1:%d", cfg.Server.Port), toolURL: "http://127.0.0.1:8094", client: newChatHTTPClient(), tasks: make(map[string]context.CancelFunc)}
	h.startStorageMaintenance()
	return h
}

type chatImage struct {
	AssetID   string `json:"assetID,omitempty"`
	ClientID  string `json:"clientID,omitempty"`
	Name      string `json:"name"`
	MediaType string `json:"mediaType"`
	URL       string `json:"url,omitempty"`
}
type chatMessage struct {
	Tools             []service.InternalToolEvent `json:"tools,omitempty"`
	ID                string                      `json:"id"`
	Role              string                      `json:"role"`
	Content           string                      `json:"content"`
	Reasoning         string                      `json:"reasoning,omitempty"`
	Model             string                      `json:"model,omitempty"`
	Effort            string                      `json:"effort,omitempty"`
	ServiceTier       string                      `json:"serviceTier,omitempty"`
	Error             string                      `json:"error,omitempty"`
	Images            []chatImage                 `json:"images,omitempty"`
	RecognitionImages []string                    `json:"recognitionImages,omitempty"`
}
type chatRecord struct {
	Generation *chatGeneration `json:"generation,omitempty"`
	ID         string          `json:"id"`
	Title      string          `json:"title"`
	Model      string          `json:"model"`
	Updated    int64           `json:"updated"`
	Version    int64           `json:"version"`
	Messages   []chatMessage   `json:"messages,omitempty"`
}

func chatUser(c *gin.Context) (int64, bool) {
	c.Header("Cache-Control", "private, no-store")
	subject, ok := middleware.GetAuthSubjectFromContext(c)
	if !ok {
		response.Unauthorized(c, "请先登录")
		return 0, false
	}
	return subject.UserID, true
}
func chatUUID(c *gin.Context) bool {
	if _, err := uuid.Parse(c.Param("id")); err != nil {
		response.BadRequest(c, "无效的对话或图片编号")
		return false
	}
	return true
}
func (h *ChatHistoryHandler) List(c *gin.Context) {
	uid, ok := chatUser(c)
	if !ok {
		return
	}
	rows, err := h.db.QueryContext(c.Request.Context(), `SELECT id,title,model,(EXTRACT(EPOCH FROM updated_at)*1000)::bigint,version FROM chat_conversations WHERE user_id=$1 AND deleted_at IS NULL ORDER BY updated_at DESC LIMIT 500`, uid)
	if err != nil {
		response.ErrorFrom(c, err)
		return
	}
	defer func() { _ = rows.Close() }()
	out := make([]chatRecord, 0)
	for rows.Next() {
		var item chatRecord
		if err = rows.Scan(&item.ID, &item.Title, &item.Model, &item.Updated, &item.Version); err != nil {
			response.ErrorFrom(c, err)
			return
		}
		out = append(out, item)
	}
	if err = rows.Err(); err != nil {
		response.ErrorFrom(c, err)
		return
	}
	sum := sha256.Sum256(mustChatJSON(out))
	etag := fmt.Sprintf(`"%x"`, sum[:16])
	c.Header("ETag", etag)
	c.Header("Cache-Control", "private, no-cache")
	if c.GetHeader("If-None-Match") == etag {
		c.Status(http.StatusNotModified)
		return
	}
	response.Success(c, out)
}
func mustChatJSON(v any) []byte { b, _ := json.Marshal(v); return b }
func (h *ChatHistoryHandler) Get(c *gin.Context) {
	uid, ok := chatUser(c)
	if !ok || !chatUUID(c) {
		return
	}
	var item chatRecord
	err := h.loadChat(c.Request.Context(), uid, c.Param("id"), &item)
	if errors.Is(err, sql.ErrNoRows) {
		response.NotFound(c, "对话不存在")
		return
	}
	if err != nil {
		response.ErrorFrom(c, err)
		return
	}
	sequence := int64(0)
	if item.Generation != nil {
		sequence = item.Generation.Sequence
	}
	etag := fmt.Sprintf(`"%d-%d"`, item.Version, sequence)
	c.Header("ETag", etag)
	c.Header("Cache-Control", "private, no-cache")
	if c.GetHeader("If-None-Match") == etag {
		c.Status(304)
		return
	}
	response.Success(c, item)
}
func validChatRecord(item *chatRecord) bool {
	if len(mustChatJSON(item.Messages)) > 12*1024*1024 {
		return false
	}
	if utf8.RuneCountInString(item.Title) > 120 || strings.TrimSpace(item.Title) == "" || len(item.Model) > 200 || len(item.Messages) > 1000 || item.Version < 0 {
		return false
	}
	for _, m := range item.Messages {
		if (m.Role != "user" && m.Role != "assistant") || len(m.ID) > 100 || len(m.Content) > 2*1024*1024 || len(m.Reasoning) > 2*1024*1024 || len(m.Model) > 200 || len(m.Effort) > 20 || len(m.ServiceTier) > 20 || len(m.Error) > 4000 || len(m.Images)+len(m.RecognitionImages) > chatMaxRecognitionImages {
			return false
		}
		if len(m.RecognitionImages) > 0 && m.Role != "user" {
			return false
		}
		seen := make(map[string]bool)
		for _, key := range m.RecognitionImages {
			if _, err := uuid.Parse(strings.TrimPrefix(key, "browser:")); err != nil || seen[key] {
				return false
			}
			seen[key] = true
		}
		if len(m.Tools) > 16 {
			return false
		}
		for _, tool := range m.Tools {
			if !validChatToolEvent(tool) || len(tool.Output) > 2*1024*1024 {
				return false
			}
		}
		for _, im := range m.Images {
			imageID := im.AssetID
			if im.ClientID != "" {
				if im.AssetID != "" {
					return false
				}
				imageID = im.ClientID
			}
			if _, err := uuid.Parse(imageID); err != nil || len(im.Name) > 500 || im.URL != "" {
				return false
			}
			if im.MediaType != "image/png" && im.MediaType != "image/jpeg" && im.MediaType != "image/webp" {
				return false
			}
		}
	}
	return true
}
func (h *ChatHistoryHandler) Put(c *gin.Context) {
	uid, ok := chatUser(c)
	if !ok || !chatUUID(c) {
		return
	}
	c.Request.Body = http.MaxBytesReader(c.Writer, c.Request.Body, 12*1024*1024)
	var item chatRecord
	if err := c.ShouldBindJSON(&item); err != nil || !validChatRecord(&item) {
		response.BadRequest(c, "对话格式无效或内容过大")
		return
	}
	item.ID = c.Param("id")
	tx, err := h.db.BeginTx(c.Request.Context(), nil)
	if err != nil {
		response.ErrorFrom(c, err)
		return
	}
	defer func() { _ = tx.Rollback() }()
	if err = h.saveChatTx(c.Request.Context(), tx, uid, &item); err != nil {
		chatSaveError(c, err)
		return
	}
	if err = tx.Commit(); err != nil {
		response.ErrorFrom(c, err)
		return
	}
	item.Messages = nil
	response.Success(c, item)
}

var errChatConflict = errors.New("这段对话已在其他页面更新或正在生成，请刷新后继续")
var errChatImage = errors.New("图片不存在，请重新上传")

func chatSaveError(c *gin.Context, err error) {
	if errors.Is(err, errChatConflict) {
		response.Error(c, 409, err.Error())
	} else if errors.Is(err, errChatImage) {
		response.BadRequest(c, err.Error())
	} else {
		response.ErrorFrom(c, err)
	}
}
func (h *ChatHistoryHandler) saveChatTx(ctx context.Context, tx *sql.Tx, uid int64, item *chatRecord) error {
	imageIDs := make(map[string]bool)
	for _, m := range item.Messages {
		for _, im := range m.Images {
			if im.AssetID != "" {
				imageIDs[im.AssetID] = true
			}
		}
	}
	for assetID := range imageIDs {
		var exists string
		err := tx.QueryRowContext(ctx, `SELECT id FROM chat_images WHERE user_id=$1 AND id=$2 FOR KEY SHARE`, uid, assetID).Scan(&exists)
		if errors.Is(err, sql.ErrNoRows) {
			return errChatImage
		}
		if err != nil {
			return err
		}
	}
	raw := mustChatJSON(item.Messages)
	if item.Messages == nil {
		raw = []byte("[]")
	}
	var err error
	if item.Version == 0 {
		err = tx.QueryRowContext(ctx, `INSERT INTO chat_conversations(user_id,id,title,model,messages) VALUES($1,$2,$3,$4,$5) ON CONFLICT DO NOTHING RETURNING version,(EXTRACT(EPOCH FROM updated_at)*1000)::bigint`, uid, item.ID, item.Title, item.Model, string(raw)).Scan(&item.Version, &item.Updated)
	} else {
		// Row locking plus the version predicate makes concurrent browser writes deterministic.
		err = tx.QueryRowContext(ctx, `UPDATE chat_conversations c SET title=$3,model=$4,messages=$5,version=version+1,updated_at=NOW() WHERE user_id=$1 AND id=$2 AND version=$6 AND deleted_at IS NULL AND NOT EXISTS(SELECT 1 FROM chat_generations g WHERE g.user_id=c.user_id AND g.conversation_id=c.id AND g.status='running') RETURNING version,(EXTRACT(EPOCH FROM updated_at)*1000)::bigint`, uid, item.ID, item.Title, item.Model, string(raw), item.Version).Scan(&item.Version, &item.Updated)
	}
	if errors.Is(err, sql.ErrNoRows) {
		return errChatConflict
	}
	if err != nil {
		return err
	}
	if _, err = tx.ExecContext(ctx, `DELETE FROM chat_conversation_images WHERE user_id=$1 AND conversation_id=$2`, uid, item.ID); err != nil {
		return err
	}
	for assetID := range imageIDs {
		if _, err = tx.ExecContext(ctx, `INSERT INTO chat_conversation_images(user_id,conversation_id,image_id) VALUES($1,$2,$3)`, uid, item.ID, assetID); err != nil {
			return err
		}
	}
	return nil
}
func (h *ChatHistoryHandler) Delete(c *gin.Context) {
	uid, ok := chatUser(c)
	if !ok || !chatUUID(c) {
		return
	}
	// Keep a tombstone so a delayed browser save cannot resurrect a deleted chat.
	tx, err := h.db.BeginTx(c.Request.Context(), nil)
	if err != nil {
		response.ErrorFrom(c, err)
		return
	}
	defer func() { _ = tx.Rollback() }()
	// Use the same account lock as Generate so another browser cannot start a
	// billed worker between the active-job check and the deletion commit.
	if _, err = tx.ExecContext(c.Request.Context(), `SELECT pg_advisory_xact_lock($1)`, uid); err != nil {
		response.ErrorFrom(c, err)
		return
	}
	var active bool
	if err := tx.QueryRowContext(c.Request.Context(), `SELECT EXISTS(SELECT 1 FROM chat_generations WHERE user_id=$1 AND conversation_id=$2 AND status='running')`, uid, c.Param("id")).Scan(&active); err != nil {
		response.ErrorFrom(c, err)
		return
	}
	if active {
		response.Error(c, 409, "请先停止生成再删除对话")
		return
	}
	if _, err = tx.ExecContext(c.Request.Context(), `UPDATE chat_conversations SET messages='[]',deleted_at=NOW(),updated_at=NOW(),version=version+1 WHERE user_id=$1 AND id=$2 AND deleted_at IS NULL`, uid, c.Param("id")); err != nil {
		response.ErrorFrom(c, err)
		return
	}
	if _, err = tx.ExecContext(c.Request.Context(), `UPDATE chat_images SET last_used_at=NOW() WHERE user_id=$1 AND id IN (SELECT image_id FROM chat_conversation_images WHERE user_id=$1 AND conversation_id=$2)`, uid, c.Param("id")); err != nil {
		response.ErrorFrom(c, err)
		return
	}
	if _, err = tx.ExecContext(c.Request.Context(), `DELETE FROM chat_conversation_images WHERE user_id=$1 AND conversation_id=$2`, uid, c.Param("id")); err != nil {
		response.ErrorFrom(c, err)
		return
	}
	if err = tx.Commit(); err != nil {
		response.ErrorFrom(c, err)
		return
	}
	response.Success(c, gin.H{"deleted": true})
}
func (h *ChatHistoryHandler) UploadImage(c *gin.Context) {
	uid, ok := chatUser(c)
	if !ok {
		return
	}
	c.Request.Body = http.MaxBytesReader(c.Writer, c.Request.Body, 7*1024*1024)
	var body struct {
		URL string `json:"url"`
	}
	if c.ShouldBindJSON(&body) != nil {
		response.BadRequest(c, "图片过大或格式无效")
		return
	}
	header, data, ok := strings.Cut(body.URL, ",")
	if !ok || !strings.HasSuffix(header, ";base64") {
		response.BadRequest(c, "无效的图片")
		return
	}
	media := strings.TrimSuffix(strings.TrimPrefix(header, "data:"), ";base64")
	raw, err := base64.StdEncoding.DecodeString(data)
	if err != nil || len(raw) == 0 || len(raw) > 5*1024*1024 || (media != "image/png" && media != "image/jpeg" && media != "image/webp") || http.DetectContentType(raw) != media {
		response.BadRequest(c, "请选择 5 MB 以内的 PNG、JPG 或 WebP 图片")
		return
	}
	sum := sha256.Sum256(raw)
	assetID := uuid.NewString()
	err = h.db.QueryRowContext(c.Request.Context(), `INSERT INTO chat_images(user_id,id,digest,media_type,content) VALUES($1,$2,$3,$4,$5) ON CONFLICT(user_id,digest) DO UPDATE SET last_used_at=NOW() RETURNING id`, uid, assetID, hex.EncodeToString(sum[:]), media, raw).Scan(&assetID)
	if err != nil {
		response.ErrorFrom(c, err)
		return
	}
	// A 24-hour grace period preserves interrupted uploads; referenced assets are never collected.
	if _, err = h.db.ExecContext(c.Request.Context(), `DELETE FROM chat_images i WHERE i.user_id=$1 AND i.last_used_at<NOW()-INTERVAL '24 hours' AND NOT EXISTS(SELECT 1 FROM chat_conversation_images r WHERE r.user_id=i.user_id AND r.image_id=i.id)`, uid); err != nil {
		response.ErrorFrom(c, err)
		return
	}
	response.Success(c, gin.H{"assetID": assetID})
}
func (h *ChatHistoryHandler) Image(c *gin.Context) {
	uid, ok := chatUser(c)
	if !ok || !chatUUID(c) {
		return
	}
	var media string
	var raw []byte
	err := h.db.QueryRowContext(c.Request.Context(), `SELECT media_type,content FROM chat_images WHERE user_id=$1 AND id=$2`, uid, c.Param("id")).Scan(&media, &raw)
	if errors.Is(err, sql.ErrNoRows) {
		response.NotFound(c, "图片不存在")
		return
	}
	if err != nil {
		response.ErrorFrom(c, err)
		return
	}
	// Authorization is checked on every request. The account-scoped app cache avoids repeat downloads.
	c.Header("X-Content-Type-Options", "nosniff")
	c.Data(200, media, raw)
}
