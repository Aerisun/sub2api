package service

import (
	"strings"

	"github.com/tidwall/gjson"
)

// Only the dedicated recognition endpoint uses this stricter completion
// contract. Ordinary Chat Completions retain their provider compatibility.
func observeRecognitionCompletion(payload string, finished, usage, failed bool) (bool, bool, bool) {
	if payload == "[DONE]" {
		return finished, usage, failed
	}
	if !gjson.Valid(payload) {
		return finished, usage, true
	}
	if v := gjson.Get(payload, "error"); v.Exists() && v.Type != gjson.Null {
		failed = true
	}
	if gjson.Get(payload, "usage.total_tokens").Int() > 0 ||
		gjson.Get(payload, "usage.prompt_tokens").Int()+gjson.Get(payload, "usage.completion_tokens").Int() > 0 {
		usage = true
	}
	for _, choice := range gjson.Get(payload, "choices").Array() {
		if choice.Get("index").Int() == 0 {
			switch strings.TrimSpace(choice.Get("finish_reason").String()) {
			case "stop", "length":
				finished = true
			}
		}
	}
	return finished, usage, failed
}
