//go:build unit

package service

import (
	"context"
	"testing"
	"time"

	"github.com/stretchr/testify/require"
)

func TestNewSignupPersistsAccessPolicy(t *testing.T) {
	for _, source := range []string{"email", "legacy_oauth", "github", "oauth_token_pair", "oauth_email", "verified_oauth_email"} {
		t.Run(source, func(t *testing.T) {
			repo := &userRepoStub{nextID: 45}
			s := newAuthService(repo, map[string]string{
				SettingKeyRegistrationEnabled: "true",
				SettingKeyDefaultConcurrency:  "5",
			}, &emailCacheStub{data: &VerificationCodeData{
				Code: "246810", CreatedAt: time.Now(), ExpiresAt: time.Now().Add(15 * time.Minute),
			}}, nil)
			s.refreshTokenCache = &refreshTokenCacheStub{}
			s.cfg.Default.UserOpenAIConcurrency = 2
			s.cfg.Default.UserAllowedGroups = []int64{103}
			s.cfg.Default.UserRestrictPublicGroups = true
			var user *User
			var err error
			switch source {
			case "email":
				_, user, err = s.Register(context.Background(), "signup@example.com", "password")
			case "legacy_oauth":
				_, user, err = s.LoginOrRegisterOAuth(context.Background(), "signup@example.com", "customer")
			case "github":
				user, err = s.createEmailOAuthUser(context.Background(), "signup@example.com", "customer", "github", "", "")
			case "oauth_token_pair":
				_, user, err = s.LoginOrRegisterOAuthWithTokenPair(context.Background(), "signup@example.com", "customer", "", "", "linuxdo")
			case "oauth_email":
				_, user, err = s.RegisterOAuthEmailAccount(context.Background(), "signup@example.com", "password", "246810", "", "oidc")
			case "verified_oauth_email":
				_, user, err = s.RegisterVerifiedOAuthEmailAccount(context.Background(), "signup@example.com", "password", "", "google")
			}
			require.NoError(t, err)
			require.Len(t, repo.created, 1)
			require.Equal(t, 5, user.Concurrency)
			require.Equal(t, 2, user.OpenAIConcurrency)
			require.Equal(t, []int64{103}, user.AllowedGroups)
			require.True(t, user.RestrictPublicGroups)
			require.Equal(t, 2, repo.created[0].OpenAIConcurrency)
			require.Equal(t, []int64{103}, repo.created[0].AllowedGroups)

			chatRepo := &signupChatKeyRepo{}
			chat := &APIKeyService{
				apiKeyRepo:  chatRepo,
				userRepo:    &visibilityUserRepo{user: user},
				userSubRepo: &visibilitySubRepo{},
				groupRepo: &visibilityGroupRepo{groups: []Group{
					{ID: 9, Platform: PlatformComposite},
					{ID: 103, Platform: PlatformComposite, IsExclusive: true},
				}},
			}
			credential, err := chat.ChatCredential(context.Background(), user.ID)
			require.NoError(t, err, "a new VIP user must be able to initialize browser chat")
			require.True(t, IsChatKey(credential))
			require.Equal(t, int64(103), chatRepo.groupID)
		})
	}
}

type signupChatKeyRepo struct {
	APIKeyRepository
	groupID int64
}

func (r *signupChatKeyRepo) EnsureChatKey(_ context.Context, _, groupID int64, candidate string) (string, error) {
	r.groupID = groupID
	return candidate, nil
}
