-- Account-scoped chat history and deduplicated image assets.
CREATE TABLE IF NOT EXISTS chat_conversations (
 user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 id UUID NOT NULL,
 title VARCHAR(120) NOT NULL,
 model VARCHAR(200) NOT NULL,
 messages JSONB NOT NULL DEFAULT '[]',
 version BIGINT NOT NULL DEFAULT 1,
 updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
 deleted_at TIMESTAMPTZ,
 PRIMARY KEY (user_id, id)
);
CREATE INDEX IF NOT EXISTS chat_conversations_user_updated ON chat_conversations(user_id, updated_at DESC) WHERE deleted_at IS NULL;
CREATE TABLE IF NOT EXISTS chat_images (
 user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 id UUID NOT NULL,
 digest TEXT NOT NULL,
 media_type VARCHAR(30) NOT NULL,
 content BYTEA NOT NULL,
 created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
 PRIMARY KEY (user_id, id),
 UNIQUE (user_id, digest)
);
ALTER TABLE chat_conversations ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE chat_images ADD COLUMN IF NOT EXISTS size_bytes INTEGER GENERATED ALWAYS AS (octet_length(content)) STORED;
CREATE INDEX IF NOT EXISTS chat_images_user_created ON chat_images(user_id,created_at);
CREATE TABLE IF NOT EXISTS chat_conversation_images (
 user_id BIGINT NOT NULL,
 conversation_id UUID NOT NULL,
 image_id UUID NOT NULL,
 PRIMARY KEY (user_id,conversation_id,image_id),
 FOREIGN KEY (user_id,conversation_id) REFERENCES chat_conversations(user_id,id) ON DELETE CASCADE,
 FOREIGN KEY (user_id,image_id) REFERENCES chat_images(user_id,id) ON DELETE RESTRICT
);
CREATE INDEX IF NOT EXISTS chat_conversation_images_asset ON chat_conversation_images(user_id,image_id);
CREATE TABLE IF NOT EXISTS chat_generations (
 user_id BIGINT NOT NULL,
 id UUID NOT NULL,
 conversation_id UUID NOT NULL,
 status VARCHAR(20) NOT NULL DEFAULT 'running',
 content TEXT NOT NULL DEFAULT '',
 reasoning TEXT NOT NULL DEFAULT '',
 error TEXT NOT NULL DEFAULT '',
 sequence BIGINT NOT NULL DEFAULT 0,
 created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
 updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
 PRIMARY KEY(user_id,id),
 FOREIGN KEY(user_id,conversation_id) REFERENCES chat_conversations(user_id,id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX IF NOT EXISTS chat_generations_active ON chat_generations(user_id,conversation_id) WHERE status='running';
CREATE INDEX IF NOT EXISTS chat_generations_conversation ON chat_generations(user_id,conversation_id,created_at DESC);
