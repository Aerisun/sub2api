package service

import (
	"bufio"
	"bytes"
	"encoding/json"
	"io"
	"strings"

	"github.com/tidwall/gjson"
)

const (
	maxOpenAIConcatenatedJSONDocuments = 16
	maxOpenAIConcatenatedJSONBytes     = 16 * 1024 * 1024
)

// splitOpenAIConcatenatedJSONDocuments recognizes the narrow corruption shape
// produced when multiple complete Responses events arrive in one transport
// message. Other malformed payloads are left untouched for normal error paths.
func splitOpenAIConcatenatedJSONDocuments(payload []byte) ([][]byte, bool) {
	payload = bytes.TrimSpace(payload)
	if len(payload) == 0 || len(payload) > maxOpenAIConcatenatedJSONBytes || json.Valid(payload) {
		return nil, false
	}

	decoder := json.NewDecoder(bytes.NewReader(payload))
	documents := make([][]byte, 0, 2)
	for {
		var raw json.RawMessage
		err := decoder.Decode(&raw)
		if err != nil {
			if err == io.EOF && len(documents) > 1 {
				return documents, true
			}
			return nil, false
		}
		raw = bytes.TrimSpace(raw)
		var envelope struct {
			Type string `json:"type"`
		}
		if err := json.Unmarshal(raw, &envelope); err != nil {
			return nil, false
		}
		eventType := strings.TrimSpace(envelope.Type)
		if eventType == "" || strings.ContainsAny(eventType, "\r\n") {
			return nil, false
		}
		if len(documents) == maxOpenAIConcatenatedJSONDocuments {
			return nil, false
		}
		documents = append(documents, raw)
	}
}

type openAISSEJSONDocumentScanner struct {
	scanner               *bufio.Scanner
	pending               []string
	current               string
	eventType             string
	terminalNeedsBoundary bool
	err                   error
}

func newOpenAISSEJSONDocumentScanner(scanner *bufio.Scanner) *openAISSEJSONDocumentScanner {
	return &openAISSEJSONDocumentScanner{scanner: scanner}
}

func (s *openAISSEJSONDocumentScanner) Scan() bool {
	if s.err != nil {
		return false
	}
	if len(s.pending) > 0 {
		s.current = s.pending[0]
		s.pending = s.pending[1:]
		return true
	}
	if s.scanner == nil || !s.scanner.Scan() {
		if s.terminalNeedsBoundary {
			// SSE clients dispatch only on a blank line. A disconnect after a
			// complete terminal JSON document must not drop that document.
			s.terminalNeedsBoundary = false
			s.current = ""
			return true
		}
		return false
	}

	line := s.scanner.Text()
	data, ok := extractOpenAISSEDataLine(line)
	if !ok {
		if line == "" {
			s.eventType = ""
			s.terminalNeedsBoundary = false
		} else if eventType, ok := extractOpenAISSEEventLine(line); ok {
			s.eventType = eventType
		}
		s.current = line
		return true
	}
	var documents [][]byte
	var repaired bool
	if len(data) <= maxOpenAIConcatenatedJSONBytes {
		documents, repaired = splitOpenAIConcatenatedJSONDocuments([]byte(data))
	}
	if !repaired {
		eventType := strings.TrimSpace(gjson.Get(data, "type").String())
		if eventType == "" {
			eventType = s.eventType
		}
		s.terminalNeedsBoundary = openAIStreamEventIsTerminalWithType(data, eventType)
		if s.terminalNeedsBoundary && strings.TrimSpace(data) != "[DONE]" && !gjson.Valid(data) {
			// A parser can extract type from an incomplete JSON prefix. Never
			// report that prefix as a successful response.
			s.terminalNeedsBoundary = false
			s.err = ErrOpenAIUpstreamStreamTruncated
			// Discard the incomplete data and end any already copied event
			// header before the relay writes its failure event.
			s.current = ""
			return true
		}
		s.current = line
		return true
	}

	s.eventType = ""
	s.terminalNeedsBoundary = false

	expanded := make([]string, 0, len(documents)*3)
	for i, document := range documents {
		if i > 0 {
			var envelope struct {
				Type string `json:"type"`
			}
			_ = json.Unmarshal(document, &envelope)
			expanded = append(expanded, "event: "+strings.TrimSpace(envelope.Type))
		}
		expanded = append(expanded, "data: "+string(document), "")
	}
	s.current = expanded[0]
	s.pending = expanded[1:]
	return true
}

func (s *openAISSEJSONDocumentScanner) Text() string {
	return s.current
}

func (s *openAISSEJSONDocumentScanner) Err() error {
	if s.err != nil {
		return s.err
	}
	if s.scanner == nil {
		return nil
	}
	return s.scanner.Err()
}
