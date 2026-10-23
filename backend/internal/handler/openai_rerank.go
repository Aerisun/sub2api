package handler

import (
	"strings"

	"github.com/tidwall/gjson"
)

// Reject invalid and unbounded pair batches before selecting an account.
func validateRerankBody(body []byte) string {
	query := gjson.GetBytes(body, "query")
	if query.Type != gjson.String || strings.TrimSpace(query.String()) == "" {
		return "query must be a non-empty string"
	}
	documents := gjson.GetBytes(body, "documents")
	if !documents.IsArray() || len(documents.Array()) == 0 {
		return "documents must be a non-empty array of strings"
	}
	items := documents.Array()
	if len(items) > 100 {
		return "documents must contain at most 100 entries"
	}
	for _, document := range items {
		if document.Type != gjson.String || strings.TrimSpace(document.String()) == "" {
			return "each document must be a non-empty string"
		}
	}
	if top := gjson.GetBytes(body, "top_n"); top.Exists() {
		if top.Type != gjson.Number || top.Float() != float64(top.Int()) || top.Int() < 1 || top.Int() > int64(len(items)) {
			return "top_n must be an integer between 1 and the document count"
		}
	}
	return ""
}
