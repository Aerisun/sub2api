package apicompat

import "encoding/json"

// ChatTokenDetails is shared with completion details, which must not acquire
// an input-only cached_tokens field. Keep an explicit zero on the prompt side
// when a breakdown was supplied, while preserving an absent breakdown as absent.
func (u ChatUsage) MarshalJSON() ([]byte, error) {
	type usageAlias ChatUsage
	type promptDetails struct {
		*ChatTokenDetails
		CachedTokens int `json:"cached_tokens"`
	}
	var prompt *promptDetails
	if u.PromptTokensDetails != nil {
		prompt = &promptDetails{u.PromptTokensDetails, u.PromptTokensDetails.CachedTokens}
	}
	return json.Marshal(struct {
		usageAlias
		PromptTokensDetails *promptDetails `json:"prompt_tokens_details,omitempty"`
	}{usageAlias(u), prompt})
}
