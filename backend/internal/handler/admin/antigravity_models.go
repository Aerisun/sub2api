package admin

import (
	"github.com/Wei-Shaw/sub2api/internal/pkg/antigravity"
	"github.com/Wei-Shaw/sub2api/internal/service"
)

func antigravityModelsForAccount(account *service.Account) []antigravity.ClaudeModel {
	// GetModelMapping itself supplies a legacy default when no custom map is
	// configured. Preserve the existing discovery fallback in that case only.
	ids := []string{}
	switch mapping := account.Credentials["model_mapping"].(type) {
	case map[string]any:
		for id, target := range mapping {
			if _, ok := target.(string); ok {
				ids = append(ids, id)
			}
		}
	case map[string]string:
		for id := range mapping {
			ids = append(ids, id)
		}
	}
	if len(ids) == 0 {
		return antigravity.DefaultModels()
	}
	return antigravity.ModelsForIDs(ids)
}
