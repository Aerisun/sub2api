package service

import (
	"context"
	"github.com/Wei-Shaw/sub2api/internal/pkg/ctxkey"
)

// UserOpenAIConcurrencyFromContext resolves the authenticated user's provider
// cap after composite routing. OpenAI-compatible wire formats alone never opt
// another platform into this budget. Zero means no additional provider cap.
func UserOpenAIConcurrencyFromContext(ctx context.Context) (int, bool) {
	if ctx == nil {
		return 0, false
	}
	limit, authenticated := ctx.Value(ctxkey.UserOpenAIConcurrency).(int)
	if !authenticated {
		return 0, false
	}
	platform, resolved := ResolvedTargetPlatformFromContext(ctx)
	if !resolved {
		if forced, ok := ctx.Value(ctxkey.ForcePlatform).(string); ok && forced != "" {
			platform = forced
		} else if group, ok := ctx.Value(ctxkey.Group).(*Group); ok && group != nil {
			platform = group.Platform
		}
	}
	return max(limit, 0), platform == PlatformOpenAI
}

type userOpenAIConcurrencyReader interface {
	GetUserOpenAIConcurrencyBatch(context.Context, []int64) (map[int64]int, error)
}

func (s *ConcurrencyService) GetUserOpenAIConcurrencyBatch(ctx context.Context, userIDs []int64) (map[int64]int, error) {
	if s == nil || s.cache == nil || len(userIDs) == 0 {
		return map[int64]int{}, nil
	}
	if cache, ok := s.cache.(userOpenAIConcurrencyReader); ok {
		return cache.GetUserOpenAIConcurrencyBatch(ctx, userIDs)
	}
	return map[int64]int{}, nil
}
