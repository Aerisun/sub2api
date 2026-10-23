package handler

import (
	"context"
	"log"
	"time"
)

// Storage is entirely in PostgreSQL, so the normal database backup captures
// transcripts, assets and in-flight checkpoints together. Cleanup never touches
// a referenced image, and a grace period protects interrupted uploads.
func (h *ChatHistoryHandler) startStorageMaintenance() {
	go func() {
		timer := time.NewTimer(10 * time.Minute)
		defer timer.Stop()
		for range timer.C {
			ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
			_, err := h.db.ExecContext(ctx, `DELETE FROM chat_images WHERE (user_id,id) IN (SELECT i.user_id,i.id FROM chat_images i WHERE i.last_used_at<NOW()-INTERVAL '24 hours' AND NOT EXISTS(SELECT 1 FROM chat_conversation_images r WHERE r.user_id=i.user_id AND r.image_id=i.id) ORDER BY i.last_used_at LIMIT 1000 FOR UPDATE OF i SKIP LOCKED)`)
			if err != nil {
				log.Printf("[ChatStorage] unused image cleanup failed: %v", err)
			}
			_, err = h.db.ExecContext(ctx, `DELETE FROM chat_generations WHERE status IN ('completed','stopped') AND updated_at<NOW()-INTERVAL '90 days'`)
			if err != nil {
				log.Printf("[ChatStorage] generation metadata cleanup failed: %v", err)
			}
			cancel()
			timer.Reset(6 * time.Hour)
		}
	}()
}
