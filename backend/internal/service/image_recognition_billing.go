package service

const defaultImageRecognitionPrice = 0.002

func imageRecognitionUnitPrice(group *Group) float64 {
	if price := matchGroupModelPricing(group, ToolImageRecognition); price != nil && price.PerRequestPrice != nil && *price.PerRequestPrice >= 0 {
		return *price.PerRequestPrice
	}
	return defaultImageRecognitionPrice
}

func imageRecognitionCost(key *APIKey, count int, multiplier float64) *CostBreakdown {
	if count <= 0 {
		return &CostBreakdown{BillingMode: string(BillingModeImage)}
	}
	var group *Group
	if key != nil {
		group = key.Group
	}
	total := imageRecognitionUnitPrice(group) * float64(count)
	if multiplier < 0 {
		multiplier = 0
	}
	return &CostBreakdown{TotalCost: total, ActualCost: total * multiplier, BillingMode: string(BillingModeImage)}
}
