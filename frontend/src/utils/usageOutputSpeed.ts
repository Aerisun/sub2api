import type { UsageLog } from '@/types'

// Streaming throughput excludes the first-token wait. Non-streaming records
// lack generation-only timing, so their rate is averaged over the whole call.
export function getUsageOutputSpeed(log: Pick<UsageLog, 'output_tokens' | 'duration_ms' | 'first_token_ms' | 'stream'>): number | null {
  if (!Number.isFinite(log.output_tokens) || log.output_tokens <= 0 || log.duration_ms == null || !Number.isFinite(log.duration_ms)) return null
  let elapsed = log.duration_ms
  if (log.stream && log.first_token_ms != null) {
    if (!Number.isFinite(log.first_token_ms) || log.first_token_ms < 0) return null
    elapsed -= log.first_token_ms
  }
  if (elapsed <= 0) return null
  const speed = log.output_tokens * 1000 / elapsed
  return Number.isFinite(speed) ? speed : null
}

export function formatUsageOutputSpeed(log: Pick<UsageLog, 'output_tokens' | 'duration_ms' | 'first_token_ms' | 'stream'>): string {
  const speed = getUsageOutputSpeed(log)
  return speed == null ? '—' : speed.toFixed(1)
}

export function getUsageOutputSpeedClass(speed: number | null): string {
  if (speed == null || !Number.isFinite(speed) || speed <= 0) return 'text-gray-400 dark:text-gray-500'
  if (speed < 10) return 'text-red-600 dark:text-red-400'
  if (speed < 20) return 'text-orange-600 dark:text-orange-400'
  if (speed < 30) return 'text-blue-600 dark:text-blue-400'
  return 'text-emerald-600 dark:text-emerald-400'
}
