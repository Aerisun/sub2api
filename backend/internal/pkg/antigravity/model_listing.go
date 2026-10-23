package antigravity

import (
	"sort"
	"strings"
)

// ModelsForIDs preserves known display metadata while including configured
// public aliases and models newer than the bundled static catalog.
func ModelsForIDs(ids []string) []ClaudeModel {
	known := make(map[string]ClaudeModel)
	for _, model := range DefaultModels() {
		known[model.ID] = model
	}
	seen := make(map[string]struct{})
	for _, id := range ids {
		if id = strings.TrimSpace(id); id != "" {
			seen[id] = struct{}{}
		}
	}
	ordered := make([]string, 0, len(seen))
	for id := range seen {
		ordered = append(ordered, id)
	}
	sort.Strings(ordered)
	models := make([]ClaudeModel, 0, len(ordered))
	for _, id := range ordered {
		model, ok := known[id]
		if !ok {
			model = ClaudeModel{ID: id, Type: "model", DisplayName: id}
		}
		models = append(models, model)
	}
	return models
}
