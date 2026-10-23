import { describe, expect, it } from 'vitest'
import { isGemini38RuntimeAlias } from '../upstreamModelAlias'

describe('observed Gemini runtime alias', () => {
  it('recognizes its canonical model and effort routes', () => {
    for (const suffix of ['', '-low', '-medium', '-high', '-tiered']) {
      expect(isGemini38RuntimeAlias(`gemini-3.8-flash${suffix}`, 'gemini-3.8-flash-n')).toBe(true)
    }
  })
  it('keeps other families, tiers and unknown suffixes distinct', () => {
    for (const response of ['gemini-3.7-flash-n', 'gemini-3.8-flash-lite-n', 'gemini-3.8-flash-high', 'gemini-3.8-flash-unknown']) {
      expect(isGemini38RuntimeAlias('gemini-3.8-flash-low', response)).toBe(false)
    }
    expect(isGemini38RuntimeAlias('claude-sonnet-4-6', 'gemini-3.8-flash-n')).toBe(false)
  })
})
