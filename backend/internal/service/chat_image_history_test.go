//go:build unit

package service

import (
	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
	"strings"
	"testing"
)

func TestHistoricalImagesNeverTriggerVisionWhileNewImagesRemain(t *testing.T) {
	for _, model := range []string{"Nemotron-3.5", "glm-5.2", "glm-5.3", "glm-5.3-flash", "step-5-preview", "qwen3.8-flash", "qwen3.8-max", "kimi-k3", "deepseek-v4.1-flash"} {
		for _, protocol := range []string{"chat", "messages", "responses"} {
			account, _, _, _ := glmVisionFixture()
			account.Extra["nvidia_image_tool"] = "deepseek"
			body := []byte(`{"model":"` + model + `","messages":[{"role":"user","content":[{"type":"image_url","image_url":{"url":"https://old.example/image.png"},"cache_control":{"type":"ephemeral"}}]},{"role":"assistant","content":"图片是红色三角形"},{"role":"user","content":"给我一段代码"}]}`)
			got, err := prepareDomesticChatControls(body, body, model, protocol, account)
			require.NoError(t, err)
			require.NotContains(t, string(got), "old.example")
			require.False(t, glmRequestHasImageInput(account, got))
			require.Contains(t, string(got), "图片是红色三角形")
			require.NotContains(t, string(got), "do not re-analyze")
			require.NotContains(t, string(got), "Historical image omitted")
			require.Equal(t, "ephemeral", gjson.GetBytes(got, "messages.0.content.0.cache_control.type").String())
			current := strings.Replace(string(body), `"content":"给我一段代码"`, `"content":[{"type":"image_url","image_url":{"url":"https://new.example/image.png"}}]`, 1)
			got, err = prepareDomesticChatControls([]byte(current), []byte(current), model, protocol, account)
			require.NoError(t, err)
			require.NotContains(t, string(got), "old.example")
			require.Contains(t, string(got), "new.example")
		}
	}
}
func TestCurrentClientToolImagesAndTextPayloadsArePreserved(t *testing.T) {
	body := []byte(`{ "messages": [{"role":"user","content":"Read the file"},{"role":"assistant","content":"","tool_calls":[{"id":"read-1"}]},{"role":"tool","tool_call_id":"read-1","content":[{"type":"image_url","image_url":{"url":"data:image/png;base64,AAAA"}}]}] }`)
	got, err := omitHistoricalChatImages(body)
	require.NoError(t, err)
	require.Equal(t, string(body), string(got))
}
