import { afterEach, describe, expect, it, vi } from 'vitest'

describe('gateway URLs under the site prefix', () => {
  afterEach(() => { vi.unstubAllEnvs(); vi.resetModules() })

  it('uses the built site mount when an API override is absent', async () => {
    vi.stubEnv('BASE_URL', '/sub2api/')
    vi.stubEnv('VITE_API_BASE_URL', '')
    vi.resetModules()
    const { getAPIBaseURL, buildApiUrl, buildGatewayUrl } = await import('../url')
    expect(getAPIBaseURL()).toBe('/sub2api/api/v1')
    expect(buildApiUrl('/settings/public')).toBe('/sub2api/api/v1/settings/public')
    expect(buildGatewayUrl('/setup/status')).toBe(`${window.location.origin}/sub2api/setup/status`)
  })

  it('keeps setup endpoints beneath the configured API mount', async () => {
    vi.stubEnv('VITE_API_BASE_URL', '/sub2api/api/v1')
    vi.resetModules()
    const { buildGatewayUrl, buildApiUrl } = await import('../url')
    expect(buildGatewayUrl('/setup/status')).toBe(`${window.location.origin}/sub2api/setup/status`)
    expect(buildGatewayUrl('/')).toBe(`${window.location.origin}/sub2api/`)
    expect(buildApiUrl('/settings/public')).toBe('/sub2api/api/v1/settings/public')
  })

  it('preserves the root deployment gateway URL', async () => {
    vi.stubEnv('VITE_API_BASE_URL', '/api/v1')
    vi.resetModules()
    const { buildGatewayUrl } = await import('../url')
    expect(buildGatewayUrl('/setup/status')).toBe(`${window.location.origin}/setup/status`)
  })

  it('keeps the configured remote API origin and prefix', async () => {
    vi.stubEnv('VITE_API_BASE_URL', 'https://example.com/sub2api/api/v1')
    vi.resetModules()
    const { buildGatewayUrl } = await import('../url')
    expect(buildGatewayUrl('/setup/status')).toBe('https://example.com/sub2api/setup/status')
  })
})
