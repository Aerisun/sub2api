package config

import (
	"testing"

	"github.com/stretchr/testify/require"
)

func TestLoadSignupAccessFromEnvironment(t *testing.T) {
	resetViperWithJWTSecret(t)
	t.Setenv("DEFAULT_USER_OPENAI_CONCURRENCY", "2")
	t.Setenv("DEFAULT_USER_ALLOWED_GROUPS", "10,11")
	t.Setenv("DEFAULT_USER_RESTRICT_PUBLIC_GROUPS", "true")
	cfg, err := Load()
	require.NoError(t, err)
	require.Equal(t, 2, cfg.Default.UserOpenAIConcurrency)
	require.Equal(t, []int64{10, 11}, cfg.Default.UserAllowedGroups)
	require.True(t, cfg.Default.UserRestrictPublicGroups)
}

func TestLoadSignupAccessRejectsInvalidPolicy(t *testing.T) {
	for _, tc := range []struct {
		name, field string
		values      map[string]string
	}{
		{"negative concurrency", "default.user_openai_concurrency", map[string]string{"DEFAULT_USER_OPENAI_CONCURRENCY": "-1"}},
		{"invalid group", "default.user_allowed_groups", map[string]string{"DEFAULT_USER_ALLOWED_GROUPS": "0"}},
		{"restricted without group", "default.user_restrict_public_groups", map[string]string{"DEFAULT_USER_RESTRICT_PUBLIC_GROUPS": "true", "DEFAULT_USER_ALLOWED_GROUPS": ""}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			resetViperWithJWTSecret(t)
			for key, value := range tc.values {
				t.Setenv(key, value)
			}
			_, err := Load()
			require.ErrorContains(t, err, tc.field)
		})
	}
}
