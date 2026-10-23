package handler

import (
	"bufio"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"strings"

	"github.com/Wei-Shaw/sub2api/internal/service"
)

type chatRecognitionResult struct {
	Name   string `json:"name"`
	Status string `json:"status"`
	Cached bool   `json:"cached"`
	Output struct {
		Text       string          `json:"text"`
		ImageCount int             `json:"image_count"`
		Metadata   json.RawMessage `json:"metadata"`
	} `json:"output"`
	Error struct {
		Message string `json:"message"`
	} `json:"error"`
}

// Only real content deltas enter the tool output. The main chat model waits
// for the final authenticated result; interrupted text is never treated as done.
func readChatRecognitionResponse(response *http.Response, local service.InternalToolEvent, emit func(chatDelta) bool) (chatRecognitionResult, error) {
	var result chatRecognitionResult
	const outputLimit = 2 * 1024 * 1024
	invalid := errors.New("识图工具返回内容不完整")
	if response.StatusCode != http.StatusOK || !strings.Contains(response.Header.Get("Content-Type"), "text/event-stream") {
		body, err := io.ReadAll(io.LimitReader(response.Body, outputLimit+1))
		if err != nil || len(body) > outputLimit {
			return result, invalid
		}
		if json.Unmarshal(body, &result) != nil {
			return result, errors.New("识图工具返回格式无效")
		}
		return result, nil
	}
	limited := &io.LimitedReader{R: response.Body, N: 8*1024*1024 + 1}
	scanner := bufio.NewScanner(limited)
	scanner.Buffer(make([]byte, 4096), outputLimit+64*1024)
	event, remoteID := "", ""
	var data []string
	var partial strings.Builder
	complete := false
	process := func() error {
		if len(data) == 0 {
			return nil
		}
		raw := []byte(strings.Join(data, "\n"))
		switch event {
		case "aerisun.tool":
			var update service.InternalToolEvent
			if json.Unmarshal(raw, &update) != nil || !validChatToolEvent(update) || update.Name != "image_recognition" || update.Status != "running" || complete {
				return invalid
			}
			if remoteID == "" {
				remoteID = update.ID
			}
			if remoteID != update.ID {
				return invalid
			}
			if update.Delta != "" {
				if partial.Len()+len(update.Delta) > outputLimit {
					return invalid
				}
				_, _ = partial.WriteString(update.Delta)
				current := local
				current.Delta = update.Delta
				if !emit(chatDelta{Tool: &current}) {
					return errors.New("识图已停止")
				}
			}
		case "result":
			if complete || json.Unmarshal(raw, &result) != nil || len(result.Output.Text) > outputLimit {
				return invalid
			}
			if partial.Len() > 0 && result.Output.Text != partial.String() {
				return invalid
			}
			complete = true
		case "error":
			var failure chatRecognitionResult
			if json.Unmarshal(raw, &failure) != nil || strings.TrimSpace(failure.Error.Message) == "" || len(failure.Error.Message) > 4000 {
				return invalid
			}
			return errors.New(failure.Error.Message)
		default:
			return invalid
		}
		return nil
	}
	for scanner.Scan() {
		line := scanner.Text()
		if line == "" {
			if err := process(); err != nil {
				return result, err
			}
			event = ""
			data = nil
		} else if strings.HasPrefix(line, "event:") {
			event = strings.TrimSpace(strings.TrimPrefix(line, "event:"))
		} else if strings.HasPrefix(line, "data:") {
			data = append(data, strings.TrimPrefix(strings.TrimPrefix(line, "data:"), " "))
		}
	}
	if scanner.Err() != nil || limited.N <= 0 || len(data) > 0 || !complete {
		return result, invalid
	}
	return result, nil
}
