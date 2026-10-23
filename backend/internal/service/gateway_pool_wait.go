package service

import (
	"context"
	"errors"
	"time"
)

// SelectAccountWithPoolWait rechecks the entire native OAuth pool while slots
// are busy. Waiting on only one account can time out despite another account
// becoming idle, especially after upstream failover during a full-pool burst.
// Existing sticky preference, exclusions, pricing gates and slot acquisition
// remain authoritative on every attempt. Other providers keep their wait plan.
func (s *GatewayService) SelectAccountWithPoolWait(ctx context.Context, groupID *int64, sessionHash, model string, excluded map[int64]struct{}, metadataUserID string, userID int64) (*AccountSelectionResult, error) {
	selection, err := s.SelectAccountWithLoadAwareness(ctx, groupID, sessionHash, model, excluded, metadataUserID, userID)
	shortReset := err == ErrNoAvailableAccounts && s.hasShortNativeModelReset(ctx, groupID, model, excluded)
	if !shortReset && (err != nil || selection == nil || selection.Acquired || selection.WaitPlan == nil || selection.Account == nil || selection.Account.Platform != PlatformAntigravity) {
		return selection, err
	}
	// This bounds slot waiting, not model generation or network response time.
	budget := 3 * time.Second
	if selection != nil && selection.WaitPlan != nil {
		budget = min(selection.WaitPlan.Timeout, budget)
	}
	lastErr := err
	deadline := time.Now().Add(budget)
	for {
		remaining := time.Until(deadline)
		if remaining <= 0 {
			if lastErr != nil {
				return nil, lastErr
			}
			// Preserve the handler's established concurrency error/stream format.
			// Its final immediate acquisition may still win a just-released slot.
			plan := *selection.WaitPlan
			plan.Timeout = time.Millisecond
			selection.WaitPlan = &plan
			return selection, nil
		}
		timer := time.NewTimer(min(remaining, 50*time.Millisecond))
		select {
		case <-ctx.Done():
			timer.Stop()
			return nil, ctx.Err()
		case <-timer.C:
		}
		// A full sticky account must not hide idle alternatives (Claude also
		// uses sticky wait plans). Skip only that account for this poll; never
		// permanently exclude an account just because its slots were busy.
		pollExcluded := make(map[int64]struct{}, len(excluded)+1)
		for id := range excluded {
			pollExcluded[id] = struct{}{}
		}
		if selection != nil && selection.Account != nil {
			pollExcluded[selection.Account.ID] = struct{}{}
		}
		next, nextErr := s.SelectAccountWithLoadAwareness(ctx, groupID, sessionHash, model, pollExcluded, metadataUserID, userID)
		if errors.Is(nextErr, ErrNoAvailableAccounts) {
			// A single eligible account may be the busy one just skipped.
			next, nextErr = s.SelectAccountWithLoadAwareness(ctx, groupID, sessionHash, model, excluded, metadataUserID, userID)
		}
		if nextErr != nil {
			if nextErr == ErrNoAvailableAccounts {
				lastErr = nextErr
				continue
			}
			return nil, nextErr
		}
		lastErr = nil
		if next == nil || next.Acquired || next.WaitPlan == nil {
			return next, nil
		}
		selection = next
	}
}

// Only a configured native model with a short, explicit provider reset is
// eligible for initial-empty-pool waiting. Missing models, disabled accounts,
// long quota holds and pricing restrictions keep their immediate rejection.
func (s *GatewayService) hasShortNativeModelReset(ctx context.Context, groupID *int64, model string, excluded map[int64]struct{}) bool {
	group, resolvedGroupID, err := s.checkClaudeCodeRestriction(ctx, groupID)
	if err != nil {
		return false
	}
	platform, forced, err := s.resolvePlatform(ctx, resolvedGroupID, group, model)
	if err != nil || platform != PlatformAntigravity {
		return false
	}
	accounts, _, err := s.listSchedulableAccounts(ctx, resolvedGroupID, platform, forced)
	if err != nil {
		return false
	}
	for i := range accounts {
		account := &accounts[i]
		if _, skip := excluded[account.ID]; skip || !account.IsSchedulable() || !s.isModelSupportedByAccountWithContext(ctx, account, model) {
			continue
		}
		remaining := account.GetRateLimitRemainingTimeWithContext(ctx, model)
		if remaining > 0 && remaining <= 3*time.Second {
			return true
		}
	}
	return false
}
