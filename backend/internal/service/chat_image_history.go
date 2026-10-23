package service

import (
	"encoding/json"
	"github.com/tidwall/gjson"
	"github.com/tidwall/sjson"
)

// This is an attachment placeholder, not a user instruction. Historical image
// descriptions already live in the conversation; routing policy stays in code.
const historicalImageText = "[图片]"

// Only images introduced by the current user message (and subsequent client
// tool results) can trigger vision. An expired cache never reopens old images.
func omitHistoricalChatImages(body []byte) ([]byte, error) {
	messages := gjson.GetBytes(body, "messages").Array()
	latestUser := -1
	for index, message := range messages {
		if message.Get("role").String() == "user" {
			latestUser = index
		}
	}
	if latestUser <= 0 {
		return body, nil
	}
	anyChanged := false
	var rewritten []json.RawMessage
	for index, message := range messages {
		value := []byte(message.Raw)
		if index < latestUser {
			parts := message.Get("content").Array()
			changed := false
			output := make([]json.RawMessage, 0, len(parts))
			for _, part := range parts {
				switch part.Get("type").String() {
				case "image_url", "image", "input_image":
					marker := map[string]any{"type": "text", "text": historicalImageText}
					if control := part.Get("cache_control"); control.Exists() {
						marker["cache_control"] = json.RawMessage(control.Raw)
					}
					raw, _ := json.Marshal(marker)
					output = append(output, raw)
					changed = true
				default:
					output = append(output, json.RawMessage(part.Raw))
				}
			}
			if changed {
				anyChanged = true
				var err error
				value, err = sjson.SetBytes(value, "content", output)
				if err != nil {
					return nil, err
				}
			}
		}
		rewritten = append(rewritten, json.RawMessage(value))
	}
	// Keep byte-for-byte payloads for ordinary conversations without old images.
	if !anyChanged {
		return body, nil
	}
	original := gjson.GetBytes(body, "messages").Raw
	raw, err := json.Marshal(rewritten)
	if err != nil {
		return nil, err
	}
	if original == string(raw) {
		return body, nil
	}
	return sjson.SetRawBytes(body, "messages", raw)
}
