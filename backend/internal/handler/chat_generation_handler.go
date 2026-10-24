package handler

import (
	"bytes"
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/Wei-Shaw/sub2api/internal/pkg/response"
	"github.com/Wei-Shaw/sub2api/internal/service"
	"github.com/gin-gonic/gin"
	"github.com/google/uuid"
)

type chatGeneration struct {
	ID       string `json:"id"`
	Status   string `json:"status"`
	Sequence int64  `json:"sequence"`
	Error    string `json:"error,omitempty"`
}

func (h *ChatHistoryHandler) Generate(c *gin.Context) {
	uid, ok := chatUser(c)
	if !ok || !chatUUID(c) {
		return
	}
	c.Request.Body = http.MaxBytesReader(c.Writer, c.Request.Body, 96*1024*1024)
	var body struct {
		Chat        chatRecord                 `json:"chat"`
		RequestID   string                     `json:"requestID"`
		Params      map[string]json.RawMessage `json:"params"`
		Append      bool                       `json:"append"`
		Retry       bool                       `json:"retry"`
		Base        *chatGenerationBase        `json:"base"`
		ImageInputs map[string]string          `json:"imageInputs"`
	}
	if c.ShouldBindJSON(&body) != nil || !validChatRecord(&body.Chat) {
		response.BadRequest(c, "对话格式无效")
		return
	}
	if _, err := uuid.Parse(body.RequestID); err != nil {
		response.BadRequest(c, "无效的生成编号")
		return
	}
	item := body.Chat
	item.ID = c.Param("id")
	if len(item.Messages) < 2 || item.Messages[len(item.Messages)-1].Role != "assistant" || item.Messages[len(item.Messages)-1].ID != body.RequestID || item.Messages[len(item.Messages)-2].Role != "user" {
		response.BadRequest(c, "缺少用户消息")
		return
	}
	// A durable request ID makes retries of a lost start response idempotent.
	var exists bool
	if err := h.db.QueryRowContext(c.Request.Context(), `SELECT EXISTS(SELECT 1 FROM chat_generations WHERE user_id=$1 AND id=$2 AND conversation_id=$3)`, uid, body.RequestID, item.ID).Scan(&exists); err != nil {
		response.ErrorFrom(c, err)
		return
	}
	if exists {
		if err := h.loadChat(c.Request.Context(), uid, item.ID, &item); err != nil {
			response.ErrorFrom(c, err)
			return
		}
		response.Success(c, item)
		return
	}
	tx, err := h.db.BeginTx(c.Request.Context(), nil)
	if err != nil {
		response.ErrorFrom(c, err)
		return
	}
	defer func() { _ = tx.Rollback() }()
	// Serialize starts for an account and bound background work before entering the gateway.
	if _, err = tx.ExecContext(c.Request.Context(), `SELECT pg_advisory_xact_lock($1)`, uid); err != nil {
		response.ErrorFrom(c, err)
		return
	}
	// A process may have gone away while a cached browser kept its SSE reader.
	// Expire lost leases before counting slots so they cannot block new turns.
	if _, err = tx.ExecContext(c.Request.Context(), `UPDATE chat_generations SET status='failed',error='生成连接已中断，请重新生成',sequence=sequence+1 WHERE user_id=$1 AND status='running' AND updated_at<NOW()-INTERVAL '2 minutes'`, uid); err != nil {
		response.ErrorFrom(c, err)
		return
	}
	var activeCount int
	if err = tx.QueryRowContext(c.Request.Context(), `SELECT COUNT(*) FROM chat_generations WHERE user_id=$1 AND status='running'`, uid).Scan(&activeCount); err != nil {
		response.ErrorFrom(c, err)
		return
	}
	if activeCount >= 3 {
		response.Error(c, 429, "已有多个回复正在生成，请稍后重试")
		return
	}
	if body.Base != nil {
		if body.Base.MessageCount < 0 || body.Base.MessageCount > 1000 || len(body.Base.LastMessageID) > 100 {
			response.BadRequest(c, "无效的对话位置")
			return
		}
		if err = h.prepareChatGeneration(c.Request.Context(), tx, uid, &item, body.Base, body.Retry); err != nil {
			chatSaveError(c, err)
			return
		}
		if !validChatRecord(&item) {
			response.BadRequest(c, "对话内容过大，请新建对话")
			return
		}
	} else if body.Append {
		if len(item.Messages) != 2 || item.Version <= 0 {
			response.BadRequest(c, "无效的新增消息")
			return
		}
		var stored []byte
		err = tx.QueryRowContext(c.Request.Context(), `SELECT messages FROM chat_conversations WHERE user_id=$1 AND id=$2 AND version=$3 AND deleted_at IS NULL FOR UPDATE`, uid, item.ID, item.Version).Scan(&stored)
		if errors.Is(err, sql.ErrNoRows) {
			chatSaveError(c, errChatConflict)
			return
		}
		if err != nil {
			response.ErrorFrom(c, err)
			return
		}
		var previous []chatMessage
		if err = json.Unmarshal(stored, &previous); err != nil {
			response.ErrorFrom(c, err)
			return
		}
		if body.Retry {
			for i := len(previous) - 1; i >= 0; i-- {
				if previous[i].Role == "user" {
					previous = previous[:i]
					break
				}
			}
		}
		item.Messages = append(previous, item.Messages...)
		if !validChatRecord(&item) {
			response.BadRequest(c, "对话内容过大，请新建对话")
			return
		}
	}
	selectedImages, err := chatSelectedHistoryImages(item.Messages)
	if err != nil {
		response.BadRequest(c, err.Error())
		return
	}
	currentImages := item.Messages[len(item.Messages)-2].Images
	if chatModelHasNativeVision(item.Model) && len(currentImages) > 4 {
		response.BadRequest(c, "当前模型每次最多上传 4 张新图片")
		return
	}
	if err = validateChatImageInputs(append(append([]chatImage{}, currentImages...), selectedImages...), body.ImageInputs); err != nil {
		response.BadRequest(c, err.Error())
		return
	}
	if err = h.saveChatTx(c.Request.Context(), tx, uid, &item); err != nil {
		chatSaveError(c, err)
		return
	}
	if _, err = tx.ExecContext(c.Request.Context(), `INSERT INTO chat_generations(user_id,id,conversation_id) VALUES($1,$2,$3)`, uid, body.RequestID, item.ID); err != nil {
		response.ErrorFrom(c, err)
		return
	}
	if err = tx.Commit(); err != nil {
		response.ErrorFrom(c, err)
		return
	}
	// Browser connection lifetime does not own this generation. Forward the original
	// authenticated JWT only to this process's regular billed chat route.
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Minute)
	ctx = context.WithValue(ctx, chatImageInputsKey{}, body.ImageInputs)
	taskKey := fmt.Sprintf("%d:%s", uid, body.RequestID)
	h.mu.Lock()
	h.tasks[taskKey] = cancel
	h.mu.Unlock()
	go h.runGeneration(ctx, cancel, uid, item, body.RequestID, c.GetHeader("Authorization"), body.Params)
	item.Generation = &chatGeneration{ID: body.RequestID, Status: "running"}
	item.Messages = nil
	response.Accepted(c, item)
}
func (h *ChatHistoryHandler) loadChat(ctx context.Context, uid int64, id string, item *chatRecord) error {
	// Canonical transcript and job state are read in one snapshot. Completion cannot
	// race a refresh into observing an old transcript with a new completed status.
	_, err := h.db.ExecContext(ctx, `UPDATE chat_generations SET status='failed',error='服务连接中断，请重新生成',sequence=sequence+1 WHERE user_id=$1 AND conversation_id=$2 AND status='running' AND updated_at<NOW()-INTERVAL '2 minutes'`, uid, id)
	if err != nil {
		return err
	}
	var raw []byte
	var g chatGeneration
	var content, reasoning string
	var tools []byte
	err = h.db.QueryRowContext(ctx, `SELECT c.id,c.title,c.model,c.version,(EXTRACT(EPOCH FROM c.updated_at)*1000)::bigint,c.messages,COALESCE(g.id::text,''),COALESCE(g.status,''),COALESCE(g.sequence,0),COALESCE(g.error,''),COALESCE(g.content,''),COALESCE(g.reasoning,''),COALESCE(g.tools,'[]'::jsonb) FROM chat_conversations c LEFT JOIN LATERAL (SELECT * FROM chat_generations WHERE user_id=c.user_id AND conversation_id=c.id ORDER BY created_at DESC LIMIT 1) g ON true WHERE c.user_id=$1 AND c.id=$2 AND c.deleted_at IS NULL`, uid, id).Scan(&item.ID, &item.Title, &item.Model, &item.Version, &item.Updated, &raw, &g.ID, &g.Status, &g.Sequence, &g.Error, &content, &reasoning, &tools)
	if err != nil {
		return err
	}
	if err = json.Unmarshal(raw, &item.Messages); err != nil {
		return err
	}
	if g.ID != "" {
		item.Generation = &g
		for i := range item.Messages {
			if item.Messages[i].ID == g.ID {
				if g.Status == "running" || content != "" || reasoning != "" {
					item.Messages[i].Content = content
					item.Messages[i].Reasoning = reasoning
					_ = json.Unmarshal(tools, &item.Messages[i].Tools)
				}
				item.Messages[i].Error = g.Error
			}
		}
	}
	return nil
}
func (h *ChatHistoryHandler) StopGeneration(c *gin.Context) {
	uid, ok := chatUser(c)
	if !ok || !chatUUID(c) {
		return
	}
	parsed, _ := uuid.Parse(c.Param("id"))
	jobID := parsed.String()
	h.mu.Lock()
	cancel := h.tasks[fmt.Sprintf("%d:%s", uid, jobID)]
	h.mu.Unlock()
	var progress []chatLiveSnapshot
	if value, ok := h.liveTools.Load(fmt.Sprintf("%d:%s", uid, jobID)); ok {
		live, _ := value.(*chatToolLive) // The private map stores live tool pointers.
		snapshot, _ := live.snapshot()
		progress = append(progress, snapshot)
	}
	if cancel != nil {
		cancel()
	}
	// The database is authoritative even when a process restart lost the cancel
	// function. Persist the checkpoint and terminal status before acknowledging.
	stopped, err := h.stopChatGeneration(c.Request.Context(), uid, jobID, progress...)
	if err != nil {
		response.ErrorFrom(c, err)
		return
	}
	response.Success(c, gin.H{"stopping": false, "stopped": stopped})
}

func (h *ChatHistoryHandler) stopChatGeneration(ctx context.Context, uid int64, jobID string, progress ...chatLiveSnapshot) (bool, error) {
	tx, err := h.db.BeginTx(ctx, nil)
	if err != nil {
		return false, err
	}
	defer func() { _ = tx.Rollback() }()
	if _, err = tx.ExecContext(ctx, `SELECT pg_advisory_xact_lock($1)`, uid); err != nil {
		return false, err
	}
	var conversationID, content, reasoning, status string
	var rawTools []byte
	err = tx.QueryRowContext(ctx, `SELECT conversation_id::text,content,reasoning,tools,status FROM chat_generations WHERE user_id=$1 AND id=$2 FOR UPDATE`, uid, jobID).Scan(&conversationID, &content, &reasoning, &rawTools, &status)
	if errors.Is(err, sql.ErrNoRows) || (err == nil && status != "running") {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	tools := make([]service.InternalToolEvent, 0)
	if err = json.Unmarshal(rawTools, &tools); err != nil {
		return false, err
	}
	if len(progress) > 0 {
		// Persist even the last visible tokens between durable checkpoints.
		content, reasoning, tools = progress[0].content, progress[0].reasoning, progress[0].tools
	}
	for i := range tools {
		if tools[i].Status == "running" {
			tools[i].Status = "failed"
			tools[i].Error = "已停止生成"
		}
	}
	_, err = tx.ExecContext(ctx, `UPDATE chat_conversations SET messages=(SELECT jsonb_agg(CASE WHEN m->>'id'=$3 THEN m || jsonb_build_object('content',$4::text,'reasoning',$5::text,'tools',$6::jsonb,'error','已停止生成') ELSE m END ORDER BY n) FROM jsonb_array_elements(messages) WITH ORDINALITY AS parts(m,n)),version=version+1,updated_at=NOW() WHERE user_id=$1 AND id=$2 AND deleted_at IS NULL`, uid, conversationID, jobID, content, reasoning, string(mustChatJSON(tools)))
	if err != nil {
		return false, err
	}
	_, err = tx.ExecContext(ctx, `UPDATE chat_generations SET status='stopped',error='已停止生成',content='',reasoning='',tools='[]'::jsonb,sequence=sequence+1,updated_at=NOW() WHERE user_id=$1 AND id=$2 AND status='running'`, uid, jobID)
	if err != nil {
		return false, err
	}
	return true, tx.Commit()
}

type chatDelta struct {
	ImageRequest *chatImageRequest
	Tool         *service.InternalToolEvent
	Content      string
	Reasoning    string
	Err          error
	Done         bool
}

func (h *ChatHistoryHandler) modelStream(ctx context.Context, uid int64, item chatRecord, auth string, params map[string]json.RawMessage, out chan<- chatDelta) {
	emit := func(d chatDelta) bool {
		select {
		case out <- d:
			return true
		case <-ctx.Done():
			return false
		}
	}
	messages := make([]map[string]any, 0, len(item.Messages)-1)
	imageURLs := make(map[string]string)
	recognizedImages := chatRecognizedImages(item.Messages[:len(item.Messages)-1])
	nativeVision := chatModelHasNativeVision(item.Model)
	references := chatHistoricalImageReferences(item.Messages)
	if nativeVision {
		needsBrowser := false
		for _, reference := range references {
			if reference.Image.ClientID != "" {
				needsBrowser = true
				break
			}
		}
		if !needsBrowser {
			references = nil
		}
	}
	referenceNames := make(map[string]string, len(references))
	for _, reference := range references {
		referenceNames[chatImageKey(reference.Image)] = reference.ID
	}
	currentUser := item.Messages[len(item.Messages)-2]
	toolImages := []chatImage(nil)
	if !nativeVision {
		toolImages = currentUser.Images
	}
	description := ""
	if len(toolImages) > 0 {
		urls := make([]string, 0, len(toolImages))
		for _, image := range toolImages {
			value, err := h.chatImageURL(ctx, uid, image, imageURLs)
			if err != nil {
				emit(chatDelta{Err: err})
				return
			}
			urls = append(urls, value)
		}
		var err error
		description, err = h.recognizeChatImageBatches(ctx, toolImages, urls, currentUser.Content, auth, emit)
		if err != nil {
			emit(chatDelta{Err: err})
			return
		}
	}
	if description != "" {
		messages = append(messages, map[string]any{"role": "system", "content": chatCurrentVisionInstruction})
	}
	latestUser := -1
	for index, m := range item.Messages[:len(item.Messages)-1] {

		if m.Role == "user" {
			latestUser = index
		}
	}
	for index, m := range item.Messages[:len(item.Messages)-1] {
		// Only helper-based recognition has a reusable text substitute. Native
		// vision history must keep its original image bytes on subsequent turns;
		// replacing them with "[图片]" changes the cached prefix and loses detail.
		reuseDescription := false
		if index < latestUser && len(m.Images) > 0 {
			reuseDescription = true
			for _, image := range m.Images {
				reuseDescription = reuseDescription && recognizedImages[chatImageKey(image)]
			}
		}
		if index < latestUser && index+1 < len(item.Messages) {
			for _, tool := range item.Messages[index+1].Tools {
				if tool.Name == "image_recognition" && tool.Status == "completed" && tool.Output != "" {
					reuseDescription = true
					break
				}
			}
		}
		visionContext := ""
		if m.Role == "assistant" {
			for _, tool := range m.Tools {
				if tool.Name == "image_recognition" && tool.Status == "completed" && tool.Output != "" {
					visionContext += chatHistoryResultContext(tool, referenceNames)
				}
			}
		}
		if (m.Error != "" || m.Content == "" && len(m.Images) == 0 && len(m.RecognitionImages) == 0) && visionContext == "" {
			continue
		}
		content := m.Content
		if m.Error != "" {
			content = ""
		}
		content += visionContext
		if reuseDescription && len(m.Images) > 0 && content == "" {
			content = "[图片]"
		}
		// A later text turn must not silently resubmit a paid recognition that
		// previously failed or was stopped. Regenerate/upload explicitly retries it.
		omitFailedImages := !nativeVision && index < latestUser && index+1 < len(item.Messages) &&
			item.Messages[index+1].Role == "assistant" && item.Messages[index+1].Error != "" && !reuseDescription
		if omitFailedImages && len(m.Images) > 0 && content == "" {
			content = "[图片识别未完成]"
		}
		// Browser-only originals are not reopened by later turns, including on
		// another device. Their completed tool result remains in the transcript.
		omitLocalHistory := index < latestUser && len(m.Images) > 0 && m.Images[0].ClientID != ""
		if omitLocalHistory && !reuseDescription && content == "" {
			content = "[图片]"
		}
		msg := map[string]any{"role": m.Role, "content": content}
		if m.Role == "assistant" && m.Reasoning != "" {
			msg["reasoning_content"] = m.Reasoning
		}
		// The model can request another look through the history tool; ordinary
		// continuation reuses the completed descriptions without another charge.
		if !nativeVision && index < latestUser && len(m.Images) > 0 && content == "" {
			msg["content"] = "[历史图片]"
		}
		if index == latestUser && description != "" {
			msg["content"] = content + "\n\n[图片识别结果]\n" + description
		}
		if nativeVision && len(m.Images) > 0 && !reuseDescription && !omitFailedImages && !omitLocalHistory {
			parts := make([]any, 0, len(m.Images)+1)
			if text, ok := msg["content"].(string); ok && text != "" {
				parts = append(parts, map[string]string{"type": "text", "text": text})
			}
			for _, image := range m.Images {
				imageURL, err := h.chatImageURL(ctx, uid, image, imageURLs)
				if err != nil {
					emit(chatDelta{Err: err})
					return
				}
				parts = append(parts, map[string]any{"type": "image_url", "image_url": map[string]string{"url": imageURL}})
			}
			msg["content"] = parts
		}
		messages = append(messages, msg)
	}
	payload := map[string]any{"model": item.Model, "messages": messages, "stream": true, "stream_options": map[string]bool{"include_usage": true}}
	for _, key := range []string{"reasoning_effort", "enable_thinking", "thinking", "service_tier"} {
		if value, ok := params[key]; ok {
			payload[key] = value
		}
	}
	if err := h.runChatImageAgent(ctx, uid, item, payload, references, chatMaxRecognitionImages-len(toolImages), auth, emit); err != nil {
		emit(chatDelta{Err: err})
		return
	}
	emit(chatDelta{Done: true})
}

func (h *ChatHistoryHandler) runGeneration(ctx context.Context, cancel context.CancelFunc, uid int64, item chatRecord, jobID, auth string, params map[string]json.RawMessage) {
	defer cancel()
	defer func() { h.mu.Lock(); delete(h.tasks, fmt.Sprintf("%d:%s", uid, jobID)); h.mu.Unlock() }()
	var running bool
	if err := h.db.QueryRowContext(ctx, `SELECT EXISTS(SELECT 1 FROM chat_generations WHERE user_id=$1 AND id=$2 AND status='running')`, uid, jobID).Scan(&running); err != nil || !running {
		return
	}
	liveKey := fmt.Sprintf("%d:%s", uid, jobID)
	live := newChatToolLive()
	h.liveTools.Store(liveKey, live)
	defer func() {
		h.liveTools.Delete(liveKey)
		live.finish()
	}()
	updates := make(chan chatDelta, 32)
	go h.modelStream(ctx, uid, item, auth, params, updates)
	ticker := time.NewTicker(300 * time.Millisecond)
	defer ticker.Stop()
	content, reasoning, status, failure := "", "", "completed", ""
	var contentBuffer, reasoningBuffer strings.Builder
	var imageRequest *chatImageRequest
	tools := make([]service.InternalToolEvent, 0)
	dirty := false
	lastCheckpoint := time.Now()
	checkpoints := newChatCheckpointWriter(ctx, func(saveCtx context.Context, snapshot chatCheckpoint) error {
		_, err := h.db.ExecContext(saveCtx, `UPDATE chat_generations SET content=$3,reasoning=$4,tools=$5,sequence=sequence+1,updated_at=NOW() WHERE user_id=$1 AND id=$2 AND status='running'`, uid, jobID, snapshot.content, snapshot.reasoning, string(snapshot.tools))
		return err
	})
	defer checkpoints.stop()
loop:
	for {
		select {
		case <-ctx.Done():
			status = "stopped"
			failure = "已停止生成"
			if errors.Is(ctx.Err(), context.DeadlineExceeded) {
				status = "failed"
				failure = "生成超时，可重新生成"
			}
			break loop
		case checkpointErr := <-checkpoints.failed:
			if checkpointErr == nil {
				continue
			}
			status = "failed"
			failure = "保存回复失败，请稍后重试"
			break loop
		case <-ticker.C:
			if !dirty && time.Since(lastCheckpoint) < 10*time.Second {
				continue
			}
			dirty = false
			lastCheckpoint = time.Now()
			checkpoints.submit(chatCheckpoint{content: content, reasoning: reasoning, tools: mustChatJSON(tools)})
		case delta := <-updates:
			if delta.ImageRequest != nil {
				imageRequest = delta.ImageRequest
				if imageRequest.ID == "" {
					imageRequest = nil
				}
			}
			if delta.Tool != nil {
				delta.Tool.ContentOffset = utf8.RuneCountInString(content)
				delta.Tool.ReasoningOffset = utf8.RuneCountInString(reasoning)
				tools = applyChatToolEvent(tools, *delta.Tool)
			}
			if len(content)+len(reasoning)+len(delta.Content)+len(delta.Reasoning) > 8*1024*1024 {
				status = "failed"
				failure = "回复已达到长度上限"
				break loop
			}
			// Append without copying the full response for every token. Builder
			// snapshots remain immutable because only new suffixes are written.
			_, _ = contentBuffer.WriteString(delta.Content)
			_, _ = reasoningBuffer.WriteString(delta.Reasoning)
			content, reasoning = contentBuffer.String(), reasoningBuffer.String()
			dirty = true
			if delta.Tool != nil || delta.ImageRequest != nil || delta.Content != "" || delta.Reasoning != "" {
				live.publish(content, reasoning, tools, imageRequest)
			}
			if delta.Err != nil {
				status = "failed"
				failure = delta.Err.Error()
				break loop
			}
			if delta.Done {
				break loop
			}
		}
	}
	checkpoints.stop()
	saveCtx, stop := context.WithTimeout(context.Background(), 15*time.Second)
	defer stop()
	if status != "completed" {
		for index := range tools {
			if tools[index].Status == "running" {
				tools[index].Status = "failed"
				tools[index].Error = failure
			}
		}
	}
	for i := range item.Messages {
		if item.Messages[i].ID == jobID {
			item.Messages[i].Content = content
			item.Messages[i].Reasoning = reasoning
			item.Messages[i].Error = failure
			item.Messages[i].Tools = tools
		}
	}
	tx, err := h.db.BeginTx(saveCtx, nil)
	if err != nil {
		return
	}
	defer func() { _ = tx.Rollback() }()
	// Serialize finalization with stopping and starting the next turn. A late
	// worker must never overwrite a stopped transcript or a newer user message.
	if _, err = tx.ExecContext(saveCtx, `SELECT pg_advisory_xact_lock($1)`, uid); err != nil {
		return
	}
	var persistedStatus string
	if err = tx.QueryRowContext(saveCtx, `SELECT status FROM chat_generations WHERE user_id=$1 AND id=$2 FOR UPDATE`, uid, jobID).Scan(&persistedStatus); err != nil || persistedStatus != "running" {
		return
	}
	_, err = tx.ExecContext(saveCtx, `UPDATE chat_conversations SET messages=$3,version=version+1,updated_at=NOW() WHERE user_id=$1 AND id=$2 AND deleted_at IS NULL`, uid, item.ID, string(mustChatJSON(item.Messages)))
	if err != nil {
		return
	}
	_, err = tx.ExecContext(saveCtx, `UPDATE chat_generations SET content='',reasoning='',tools='[]'::jsonb,status=$3,error=$4,sequence=sequence+1,updated_at=NOW() WHERE user_id=$1 AND id=$2`, uid, jobID, status, failure)
	if err != nil {
		return
	}
	_ = tx.Commit()
}

// Only new text crosses the network while generating; the full transcript is fetched once at completion.
func (h *ChatHistoryHandler) Generation(c *gin.Context) {
	uid, ok := chatUser(c)
	if !ok || !chatUUID(c) {
		return
	}
	offset, _ := strconv.Atoi(c.Query("content_offset"))
	reasoningOffset, _ := strconv.Atoi(c.Query("reasoning_offset"))
	if offset < 0 || offset > 8*1024*1024 || reasoningOffset < 0 || reasoningOffset > 8*1024*1024 {
		response.BadRequest(c, "无效的回复位置")
		return
	}
	var g chatGeneration
	var content, reasoning string
	var tools []byte
	err := h.db.QueryRowContext(c.Request.Context(), `SELECT g.id,g.status,g.sequence,g.error,substring(g.content from $3::int),substring(g.reasoning from $4::int),g.tools FROM chat_generations g JOIN chat_conversations c ON c.user_id=g.user_id AND c.id=g.conversation_id WHERE g.user_id=$1 AND g.id=$2 AND c.deleted_at IS NULL`, uid, c.Param("id"), offset+1, reasoningOffset+1).Scan(&g.ID, &g.Status, &g.Sequence, &g.Error, &content, &reasoning, &tools)
	if errors.Is(err, sql.ErrNoRows) {
		response.NotFound(c, "回复不存在")
		return
	}
	if err != nil {
		response.ErrorFrom(c, err)
		return
	}
	etag := fmt.Sprintf(`"%d"`, g.Sequence)
	c.Header("ETag", etag)
	c.Header("Cache-Control", "private, no-cache")
	if c.GetHeader("If-None-Match") == etag {
		c.Status(304)
		return
	}
	response.Success(c, gin.H{"generation": g, "content": content, "reasoning": reasoning, "tools": json.RawMessage(tools)})
}

// Events keeps a single authenticated connection open. Disconnecting a reader
// never cancels the separately owned model worker; reconnection resumes by offset.
func (h *ChatHistoryHandler) GenerationEvents(c *gin.Context) {
	uid, ok := chatUser(c)
	if !ok || !chatUUID(c) {
		return
	}
	contentOffset, _ := strconv.Atoi(c.Query("content_offset"))
	reasoningOffset, _ := strconv.Atoi(c.Query("reasoning_offset"))
	if contentOffset < 0 || contentOffset > 8*1024*1024 || reasoningOffset < 0 || reasoningOffset > 8*1024*1024 {
		response.BadRequest(c, "无效的回复位置")
		return
	}
	c.Header("Content-Type", "text/event-stream")
	c.Header("Cache-Control", "private, no-cache, no-store, no-transform")
	c.Header("X-Accel-Buffering", "no")
	ticker := time.NewTicker(350 * time.Millisecond)
	defer ticker.Stop()
	sequence := int64(-1)
	liveVersion := uint64(0)
	contentCursor := newChatStreamCursor(contentOffset)
	reasoningCursor := newChatStreamCursor(reasoningOffset)
	lastEvent := time.Now()
	nextLeaseCheck := time.Time{}
	for {
		if time.Now().After(nextLeaseCheck) {
			if err := h.expireChatGeneration(c.Request.Context(), uid, c.Param("id")); err != nil {
				return
			}
			nextLeaseCheck = time.Now().Add(15 * time.Second)
		}
		var g chatGeneration
		var content, reasoning string
		var tools []byte
		err := h.db.QueryRowContext(c.Request.Context(), `SELECT g.id,g.status,g.sequence,g.error,substring(g.content from $3::int),substring(g.reasoning from $4::int),g.tools FROM chat_generations g JOIN chat_conversations c ON c.user_id=g.user_id AND c.id=g.conversation_id WHERE g.user_id=$1 AND g.id=$2 AND c.deleted_at IS NULL`, uid, c.Param("id"), contentOffset+1, reasoningOffset+1).Scan(&g.ID, &g.Status, &g.Sequence, &g.Error, &content, &reasoning, &tools)
		if err != nil {
			if c.Request.Context().Err() == nil {
				_, _ = fmt.Fprint(c.Writer, "data: {\"error\":\"回复连接暂时中断\"}\n\n")
				c.Writer.Flush()
			}
			return
		}
		var live *chatToolLive
		var imageRequest *chatImageRequest
		var toolChanged <-chan struct{}
		previousLiveVersion := liveVersion
		if value, ok := h.liveTools.Load(fmt.Sprintf("%d:%s", uid, c.Param("id"))); ok && g.Status == "running" {
			live, _ = value.(*chatToolLive)
			snapshot, changed := live.snapshot()
			imageRequest = snapshot.imageRequest
			toolChanged = changed
			content = contentCursor.suffix(snapshot.content)
			reasoning = reasoningCursor.suffix(snapshot.reasoning)
			tools = mustChatJSON(snapshot.tools)
			liveVersion = snapshot.version
			// In-process deltas wake the reader. Poll only for ownership, deletion
			// and externally changed status, rather than for every token.
			ticker.Reset(2 * time.Second)
		} else {
			ticker.Reset(350 * time.Millisecond)
		}
		if g.Sequence != sequence || liveVersion != previousLiveVersion || content != "" || reasoning != "" {
			data := mustChatJSON(gin.H{"generation": g, "content": content, "reasoning": reasoning, "tools": json.RawMessage(tools), "imageRequest": imageRequest})
			if _, err = fmt.Fprintf(c.Writer, "data: %s\n\n", data); err != nil {
				return
			}
			c.Writer.Flush()
			contentOffset += utf8.RuneCountInString(content)
			reasoningOffset += utf8.RuneCountInString(reasoning)
			if live == nil {
				contentCursor = newChatStreamCursor(contentOffset)
				reasoningCursor = newChatStreamCursor(reasoningOffset)
			}
			sequence = g.Sequence
			lastEvent = time.Now()
			if g.Status != "running" {
				return
			}
		} else if time.Since(lastEvent) > 15*time.Second {
			if _, err = fmt.Fprint(c.Writer, ": keepalive\n\n"); err != nil {
				return
			}
			c.Writer.Flush()
			lastEvent = time.Now()
		}
	waitForCheckpoint:
		for {
			select {
			case <-c.Request.Context().Done():
				return
			case <-ticker.C:
				break waitForCheckpoint
			case <-toolChanged:
				// Coalesce dense bursts into at most one frame per 20 ms. The
				// first semantic output is sent immediately after model waiting.
				if delay := time.Until(lastEvent.Add(20 * time.Millisecond)); delay > 0 {
					timer := time.NewTimer(delay)
					select {
					case <-c.Request.Context().Done():
						timer.Stop()
						return
					case <-timer.C:
					}
				}
				snapshot, changed := live.snapshot()
				toolChanged = changed
				if snapshot.finished {
					break waitForCheckpoint
				}
				if snapshot.version == liveVersion {
					continue
				}
				liveVersion = snapshot.version
				content = contentCursor.suffix(snapshot.content)
				reasoning = reasoningCursor.suffix(snapshot.reasoning)
				contentOffset, reasoningOffset = contentCursor.runes, reasoningCursor.runes
				data := mustChatJSON(gin.H{"generation": g, "content": content, "reasoning": reasoning, "tools": snapshot.tools, "imageRequest": snapshot.imageRequest})
				if _, err = fmt.Fprintf(c.Writer, "data: %s\n\n", data); err != nil {
					return
				}
				c.Writer.Flush()
				lastEvent = time.Now()
			}
		}
	}
}

// Only known server tools enter persisted UI state; caller-visible client tools
// are not executed or reinterpreted here.
func validChatToolEvent(event service.InternalToolEvent) bool {
	if len(event.Input) > 32*1024 || (len(event.Input) > 0 && !json.Valid(event.Input)) || len(event.Sources) > 50 ||
		len(event.Metadata) > 8*1024 || (len(event.Metadata) > 0 && (!json.Valid(event.Metadata) || !bytes.HasPrefix(bytes.TrimSpace(event.Metadata), []byte("{")))) {
		return false
	}
	contentBytes := 0
	for _, source := range event.Sources {
		link, err := url.Parse(source.URL)
		if err != nil || link.Hostname() == "" || link.User != nil || (link.Scheme != "https" && link.Scheme != "http") ||
			len(source.URL) > 4000 || len(source.ID) > 200 || len(source.Title) > 2000 || len(source.Snippet) > 8000 || len(source.PublishedAt) > 400 ||
			len(source.Content) > 32*1024 || len(source.Domain) > 1000 || len(source.SourceType) > 400 || len(source.Published) > 400 || len(source.Crawled) > 400 || len(source.ReferenceIDs) > 50 {
			return false
		}
		for _, ref := range source.ReferenceIDs {
			if len(ref) > 200 {
				return false
			}
		}
		contentBytes += len(source.Content)
	}
	if contentBytes > 128*1024 {
		return false
	}
	return event.Type == "tool_call" && event.ID != "" && len(event.ID) <= 200 &&
		(event.Name == "image_recognition" || event.Name == "image_generation" || event.Name == "web_search") &&
		(event.Status == "running" || event.Status == "completed" || event.Status == "failed") &&
		len(event.Delta) <= 2*1024*1024 && len(event.Output) <= 2*1024*1024 && len(event.Error) <= 4000
}
func applyChatToolEvent(tools []service.InternalToolEvent, event service.InternalToolEvent) []service.InternalToolEvent {
	for index := range tools {
		if tools[index].ID == event.ID {
			previous := tools[index]
			if event.Name != previous.Name {
				return tools
			}
			event.ContentOffset = previous.ContentOffset
			event.ReasoningOffset = previous.ReasoningOffset
			if len(event.Input) == 0 {
				event.Input = previous.Input
			}
			if event.Sources == nil {
				event.Sources = previous.Sources
			}
			if len(event.Metadata) == 0 {
				event.Metadata = previous.Metadata
			}
			if event.Output == "" {
				event.Output = previous.Output + event.Delta
			}
			if len(event.Output) > 2*1024*1024 {
				return tools
			}
			event.Delta = ""
			tools[index] = event
			return tools
		}
	}
	if len(tools) >= 16 {
		return tools
	}
	if event.Output == "" {
		event.Output = event.Delta
	}
	event.Delta = ""
	return append(tools, event)
}

// Heartbeats are written by the worker, never by a reader. A live SSE connection
// therefore cannot keep an abandoned generation alive after a process restart.
func (h *ChatHistoryHandler) expireChatGeneration(ctx context.Context, uid int64, jobID string) error {
	_, err := h.db.ExecContext(ctx, `UPDATE chat_generations SET status='failed',error='生成连接已中断，请重新生成',sequence=sequence+1 WHERE user_id=$1 AND id=$2 AND status='running' AND updated_at<NOW()-INTERVAL '2 minutes'`, uid, jobID)
	return err
}
