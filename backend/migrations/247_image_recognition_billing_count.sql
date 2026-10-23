-- Recognition bills input images per item, without a generated-image size.
-- Keep the existing output-size requirement for all image generation rows.
ALTER TABLE usage_logs DROP CONSTRAINT IF EXISTS usage_logs_image_billing_size_check;
ALTER TABLE usage_logs ADD CONSTRAINT usage_logs_image_billing_size_check CHECK (
    image_count <= 0
    OR billing_mode = 'video'
    OR COALESCE(video_count, 0) > 0
    OR (model = 'image_recognition' AND billing_mode = 'image' AND image_count BETWEEN 1 AND 4 AND image_size IS NULL)
    OR (image_size IS NOT NULL AND image_size IN ('1K', '2K', '4K', 'mixed'))
) NOT VALID;
