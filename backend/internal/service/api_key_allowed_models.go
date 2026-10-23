package service

import (
	"strings"
	"unicode"

	infraerrors "github.com/Wei-Shaw/sub2api/internal/pkg/errors"
)

func normalizeAPIKeyAllowedModels(models []string) ([]string, error) {
	if len(models) > 200 {
		return nil, infraerrors.BadRequest("INVALID_ALLOWED_MODELS", "Select at most 200 models")
	}
	out := make([]string, 0, len(models))
	seen := make(map[string]bool, len(models))
	for _, model := range models {
		model = strings.TrimSpace(model)
		if model == "" || len(model) > 200 || strings.Contains(model, "*") || strings.ContainsFunc(model, func(r rune) bool { return unicode.IsSpace(r) || unicode.IsControl(r) }) {
			return nil, infraerrors.BadRequest("INVALID_ALLOWED_MODELS", "Allowed models must contain exact, non-empty model IDs")
		}
		if !seen[strings.ToLower(model)] {
			out = append(out, model)
			seen[strings.ToLower(model)] = true
		}
	}
	return out, nil
}

// WithModelRestrictions returns a request-local key/group copy. Cached auth
// snapshots must never be mutated. The intersection also keeps model discovery
// and every Responses WebSocket turn consistent with HTTP admission.
func (k *APIKey) WithModelRestrictions() *APIKey {
	if k == nil || len(k.AllowedModels) == 0 || k.Group == nil {
		return k
	}
	key := *k
	group := *k.Group
	models := make([]string, 0, len(k.AllowedModels))
	for _, model := range k.AllowedModels {
		if group.ModelAllowlist.Allows(model) {
			models = append(models, model)
		}
	}
	group.ModelAllowlist = GroupModelAllowlist{Enabled: true, Models: models}
	key.Group = &group
	return &key
}
