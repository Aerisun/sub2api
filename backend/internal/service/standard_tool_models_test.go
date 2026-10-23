package service

import (
	"github.com/stretchr/testify/require"
	"testing"
)

func TestStandardToolDiscoveryAndScopedDelegation(t *testing.T) {
	models := []string{ToolVisionModel, ToolDefaultImageModel, "qwen3-embedding-4b", ToolSearchModels[0]}
	got := AppendStandardToolModels(models)
	require.Contains(t, got, ToolWebSearch)
	require.Contains(t, got, ToolImageGeneration)
	require.Contains(t, got, ToolImageRecognition)
	require.Len(t, AppendStandardToolModels(got), len(got))
	require.Len(t, models, 4)
	group := &Group{ModelAllowlist: GroupModelAllowlist{Enabled: true, Models: append(got, ToolSearchModels...)}}
	for _, tc := range []struct{ tool, path, backing string }{
		{ToolWebSearch, "/v1/alpha/search", ToolSearchModels[0]},
		{ToolImageGeneration, "/v1/images/generations", ToolDefaultImageModel},
		{ToolImageRecognition, "/v1/images/recognitions", ToolVisionModel},
	} {
		key := &APIKey{Group: group, AllowedModels: []string{tc.tool}}
		delegated := key.WithToolEndpointPermissions(tc.path).WithModelRestrictions()
		require.True(t, delegated.Group.ModelAllowlist.Allows(tc.backing), tc.tool)
		for _, path := range []string{"/v1/chat/completions", "/v1/responses", "/v1/messages", "/v1/embeddings", "/v1/realtime"} {
			unrelated := key.WithToolEndpointPermissions(path).WithModelRestrictions()
			require.False(t, unrelated.Group.ModelAllowlist.Allows(tc.backing), path)
		}
		require.Equal(t, []string{tc.tool}, key.AllowedModels, "cached credentials must not be changed")
	}
}

func TestStandardToolPlazaHidesImageProvidersAndUsesBilledPrices(t *testing.T) {
	imagePrice, inputPrice, searchPrice := 0.15, 0.0000001, 0.027
	models := []PlazaModel{
		{Name: ToolDefaultImageModel, Platform: PlatformOpenAI, Pricing: &ChannelModelPricing{BillingMode: BillingModeImage, PerRequestPrice: &imagePrice}},
		{Name: ToolVisionModel, Platform: PlatformOpenAI, Pricing: &ChannelModelPricing{BillingMode: BillingModeToken, InputPrice: &inputPrice}},
		{Name: ToolSearchModels[0], Platform: PlatformOpenAI, Pricing: &ChannelModelPricing{BillingMode: BillingModeToken, InputPrice: &inputPrice}},
		{Name: "qwen3-embedding-4b", Platform: PlatformOpenAI},
	}
	g := &Group{WebSearchPricePerCall: &searchPrice}
	s := &ModelPlazaService{}
	got := s.standardToolEntries(models, g)
	byName := map[string]PlazaModel{}
	for _, m := range got {
		byName[m.Name] = m
	}
	require.NotContains(t, byName, ToolDefaultImageModel)
	require.Equal(t, imagePrice, *byName[ToolImageGeneration].Pricing.PerRequestPrice)
	require.Equal(t, searchPrice, *byName[ToolWebSearch].Pricing.PerRequestPrice)
	require.Equal(t, 0.002, *byName[ToolImageRecognition].Pricing.PerRequestPrice)
	require.Equal(t, BillingModeImage, byName[ToolImageRecognition].Pricing.BillingMode)
	require.Len(t, byName[ToolImageGeneration].ToolModels, 1)
	require.Equal(t, "/v1/tools/image_generation", byName[ToolImageGeneration].Endpoint)
	require.Contains(t, byName, "qwen3-embedding-4b")
	require.Equal(t, ToolDefaultImageModel, models[0].Name)
}

func TestRecognitionDelegatesOnlyWithinItsEndpoint(t *testing.T) {
	for _, model := range ToolVisionModels {
		require.Contains(t, AppendStandardToolModels([]string{model}), ToolImageRecognition)
		key := &APIKey{AllowedModels: []string{ToolImageRecognition}, Group: &Group{ModelAllowlist: GroupModelAllowlist{Enabled: true, Models: append([]string{ToolImageRecognition}, ToolVisionModels...)}}}
		require.Contains(t, key.WithToolEndpointPermissions("/v1/images/recognitions").AllowedModels, model)
		for _, path := range []string{"/v1/chat/completions", "/v1/responses", "/v1/messages"} {
			require.NotContains(t, key.WithToolEndpointPermissions(path).AllowedModels, model)
		}
	}
}

func TestHiddenVisionBackendDelegationDoesNotChangePublicPermissions(t *testing.T) {
	for _, allowed := range [][]string{nil, {ToolImageRecognition}, {"glm-5.3-flash"}, {ToolWebSearch}} {
		group := &Group{ModelAllowlist: GroupModelAllowlist{Enabled: true, Models: []string{ToolImageRecognition, "glm-5.3-flash", ToolWebSearch}}}
		key := &APIKey{Group: group, AllowedModels: allowed}
		delegated := key.WithToolEndpointPermissions("/v1/images/recognitions").WithModelRestrictions()
		want := len(allowed) == 0 || allowed[0] == ToolImageRecognition
		require.Equal(t, want, delegated.Group.ModelAllowlist.Allows(ToolVisionModel))
		require.False(t, key.Group.ModelAllowlist.Allows(ToolVisionModel), "shared auth must remain unchanged")
		for _, path := range []string{"/v1/chat/completions", "/v1/responses", "/v1/models"} {
			require.False(t, key.WithToolEndpointPermissions(path).WithModelRestrictions().Group.ModelAllowlist.Allows(ToolVisionModel))
		}
	}
}
