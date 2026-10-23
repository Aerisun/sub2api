import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import ModelAccessSelector from '../ModelAccessSelector.vue'
import { modelPlazaAPI } from '@/api/modelPlaza'

vi.mock('@/api/modelPlaza', () => ({ modelPlazaAPI: { getModelPlaza: vi.fn() } }))
vi.mock('vue-i18n', () => ({ useI18n: () => ({ t: (key: string) => key }) }))
describe('model access choices', () => {
  beforeEach(() => {
    vi.mocked(modelPlazaAPI.getModelPlaza).mockResolvedValue({ groups: [{ id: 9, models: [{ name: 'claude-sonnet-4-6' }, { name: 'gemini-3.1-flash-image' }] }] } as any)
  })
  it('excludes catalog entries outside the selected group permissions', async () => {
    const wrapper = mount(ModelAccessSelector, { props: { groupId: 9, restricted: true, models: [], allowlist: { enabled: true, models: ['claude-sonnet-4-6'] } } })
    await flushPromises()
    expect(wrapper.text()).toContain('claude-sonnet-4-6')
    expect(wrapper.text()).not.toContain('gemini-3.1-flash-image')
    await wrapper.get('input[type="checkbox"]').setValue(true)
    expect(wrapper.emitted('update:models')?.[0]).toEqual([['claude-sonnet-4-6']])
  })
  it('uses exact group choices when the model plaza is unavailable', async () => {
    vi.mocked(modelPlazaAPI.getModelPlaza).mockRejectedValue(new Error('disabled'))
    const wrapper = mount(ModelAccessSelector, { props: { groupId: 9, restricted: true, models: [], allowlist: { enabled: true, models: ['claude-sonnet-4-6'] } } })
    await flushPromises()
    expect(wrapper.find('[role="alert"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('claude-sonnet-4-6')
  })
  it('expands group wildcards from the catalog and preserves unavailable saved choices', async () => {
    const wrapper = mount(ModelAccessSelector, { props: { groupId: 9, restricted: true, models: ['previous-model'], allowlist: { enabled: true, models: ['claude-*'] } } })
    await flushPromises()
    expect(wrapper.findAll('input[type="checkbox"]')).toHaveLength(2)
    expect(wrapper.text()).toContain('keys.modelUnavailable')
    expect(wrapper.text()).not.toContain('gemini-3.1-flash-image')
  })
})
