package handler

import (
	"github.com/Wei-Shaw/sub2api/internal/pkg/httputil"
	"github.com/Wei-Shaw/sub2api/internal/pkg/requestmodel"
	"github.com/Wei-Shaw/sub2api/internal/service"
	"github.com/gin-gonic/gin"
	"github.com/tidwall/gjson"
	"net/http"
	"slices"
)

// This narrow endpoint delegates recognition, never arbitrary text/tools. It
// retains the existing model forwarding, quota checks, token usage and billing.
func (h *OpenAIGatewayHandler) ImageRecognition(c *gin.Context) {
	body, err := httputil.ReadRequestBodyWithPrealloc(c.Request)
	images := 0
	if err == nil && gjson.ValidBytes(body) && slices.Contains(service.ToolVisionModels, gjson.GetBytes(body, "model").String()) {
		for _, message := range gjson.GetBytes(body, "messages").Array() {
			for _, part := range message.Get("content").Array() {
				if part.Get("type").String() == "image_url" && part.Get("image_url.url").String() != "" {
					images++
				}
			}
		}
	}
	if images != 1 || len(gjson.GetBytes(body, "tools").Array()) > 0 {
		h.errorResponse(c, http.StatusBadRequest, "invalid_request_error", "Image recognition requires exactly one image and does not accept client tools")
		return
	}
	c.Set("standard_tool.image_recognition_count", images)
	// The tool may retry a failed stream only when this gateway guarantees
	// that an incomplete recognition carries no image charge.
	c.Header("X-Aerisun-Recognition-Billing", "completed-only-v1")
	requestmodel.ResetRequestBody(c.Request, body)
	h.ChatCompletions(c)
}
