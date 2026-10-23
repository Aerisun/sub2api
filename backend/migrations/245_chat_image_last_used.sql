-- Creation time remains stable; unused-image retention follows the last reference/upload.
ALTER TABLE chat_images ADD COLUMN IF NOT EXISTS last_used_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
CREATE INDEX IF NOT EXISTS chat_images_last_used ON chat_images(last_used_at);
