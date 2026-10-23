ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS allowed_models JSONB;
COMMENT ON COLUMN api_keys.allowed_models IS 'Allowed public model IDs; NULL or [] permits all models available to the group';
