package service

import (
	"context"
	"github.com/stretchr/testify/require"
	"testing"
	"time"
)

func TestImageRecognitionFlatPriceReplacesTokens(t *testing.T) {
	s := &OpenAIGatewayService{}
	for _, count := range []int{0, 1, 2, 4} {
		result := &OpenAIForwardResult{ImageRecognition: true, ImageRecognitionCount: count}
		cost, err := s.calculateOpenAIRecordUsageCost(context.Background(), result, nil, []string{ToolVisionModel}, 1, 1, 1, 1, UsageTokens{InputTokens: 1000000, OutputTokens: 1000000}, "", nil, time.Now())
		require.NoError(t, err)
		require.Equal(t, float64(count)*0.002, cost.TotalCost)
		require.Equal(t, cost.TotalCost, cost.ActualCost)
		require.Zero(t, cost.InputCost)
		require.Zero(t, cost.OutputCost)
		require.Equal(t, string(BillingModeImage), cost.BillingMode)
	}
}
