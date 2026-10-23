import { describe, expect, it } from 'vitest'
import { formatUsageOutputSpeed, getUsageOutputSpeed, getUsageOutputSpeedClass } from '../usageOutputSpeed'

const log = { output_tokens: 100, duration_ms: 3000, first_token_ms: 1000, stream: true }
describe('output speed', () => {
  it('excludes the first-token wait only for streaming calls', () => {
    expect(getUsageOutputSpeed(log)).toBe(50)
    expect(formatUsageOutputSpeed(log)).toBe('50.0')
    expect(getUsageOutputSpeed({ ...log, stream: false })).toBeCloseTo(100 / 3)
    expect(getUsageOutputSpeed({ ...log, first_token_ms: null })).toBeCloseTo(100 / 3)
  })
  it('does not fabricate speeds from missing or invalid timings or empty output', () => {
    for (const changes of [{ duration_ms: null }, { duration_ms: 0 }, { duration_ms: -1 }, { duration_ms: NaN }, { first_token_ms: 3000 }, { first_token_ms: 4000 }, { first_token_ms: -1 }, { output_tokens: 0 }, { output_tokens: Infinity }]) {
      expect(getUsageOutputSpeed({ ...log, ...changes })).toBeNull()
      expect(formatUsageOutputSpeed({ ...log, ...changes })).toBe('—')
    }
  })
  it('colors speed tiers consistently at their boundaries', () => {
    for (const speed of [0.1, 9.99]) expect(getUsageOutputSpeedClass(speed)).toContain('text-red-600')
    for (const speed of [10, 19.99]) expect(getUsageOutputSpeedClass(speed)).toContain('text-orange-600')
    for (const speed of [20, 29.99]) expect(getUsageOutputSpeedClass(speed)).toContain('text-blue-600')
    for (const speed of [30, 1000]) expect(getUsageOutputSpeedClass(speed)).toContain('text-emerald-600')
    for (const speed of [null, NaN, Infinity, 0, -1]) expect(getUsageOutputSpeedClass(speed)).toContain('text-gray-400')
  })
})
