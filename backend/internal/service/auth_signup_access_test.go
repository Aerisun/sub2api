package service

import (
	"context"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/stretchr/testify/require"
)

func TestSignupGrantPlanAccessDefaults(t *testing.T) {
	cfg := &config.Config{Default: config.DefaultConfig{
		UserConcurrency: 5, UserOpenAIConcurrency: 2,
		UserAllowedGroups: []int64{10}, UserRestrictPublicGroups: true,
	}}
	s := &AuthService{cfg: cfg}
	for _, source := range []string{"email", "linuxdo", "google", "github", "oidc", "wechat", "dingtalk"} {
		plan := s.resolveSignupGrantPlan(context.Background(), source)
		require.Equal(t, 5, plan.Concurrency)
		require.Equal(t, 2, plan.OpenAIConcurrency)
		require.Equal(t, []int64{10}, plan.AllowedGroups)
		require.True(t, plan.RestrictPublicGroups)
		plan.AllowedGroups[0] = 999
		require.Equal(t, []int64{10}, cfg.Default.UserAllowedGroups)
	}
}

func TestSignupGrantPlanAccessDefaultsDisabled(t *testing.T) {
	s := &AuthService{cfg: &config.Config{Default: config.DefaultConfig{UserConcurrency: 5}}}
	plan := s.resolveSignupGrantPlan(context.Background(), "email")
	require.Zero(t, plan.OpenAIConcurrency)
	require.Empty(t, plan.AllowedGroups)
	require.False(t, plan.RestrictPublicGroups)
}
