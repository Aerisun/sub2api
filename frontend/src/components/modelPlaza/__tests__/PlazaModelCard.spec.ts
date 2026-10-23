import { mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'
import type { ModelPlazaGroup, PlazaModel } from '@/api/modelPlaza'
import PlazaModelCard from '../PlazaModelCard.vue'

vi.mock('vue-i18n', () => ({ useI18n: () => ({ t: (key: string) => key }) }))

const group: ModelPlazaGroup = {
  id: 9,
  name: '大一统',
  description: '',
  platform: 'composite',
  subscription_type: 'standard',
  rate_multiplier: 1,
  peak_rate_enabled: false,
  peak_start: '',
  peak_end: '',
  peak_rate_multiplier: 1,
  is_exclusive: false,
  image_rate_independent: false,
  image_rate_multiplier: 1,
  long_context_pricing_enabled: true,
  models: [],
}
function model(overrides: Partial<PlazaModel> = {}): PlazaModel {
  return {
    name: 'claude-sonnet-4-6',
    platform: 'antigravity',
    pricing: {
      billing_mode: 'token',
      input_price: 0.000003,
      output_price: 0.000015,
      cache_read_price: 0.0000003,
      cache_write_price: 0.00000375,
      cache_write_1h_price: 0.000006,
      image_input_price: null,
      image_output_price: null,
      per_request_price: null,
      intervals: [],
    },
    official_pricing: {
      input_price: 0.000099,
      output_price: 0.000999,
      cache_read_price: null,
      cache_write_price: null,
    },
    ...overrides,
  }
}
function card(m = model(), g = group) {
  return mount(PlazaModelCard, { props: { model: m, group: g } })
}

describe('Model cards display site charges', () => {
  it('shows configured input, output and cache prices without official reference prices or transport labels', () => {
    const wrapper = card()
    const text = wrapper.text()
    for (const price of ['$3.00', '$15.00', '$0.30', '$3.75', '$6.00'])
      expect(text).toContain(price)
    expect(text).toContain('Anthropic')
    expect(text).toContain('大一统')
    expect(text).not.toContain('$99')
    expect(text).not.toContain('$999')
    expect(text).not.toContain('official')
    expect(text).not.toContain('antigravity')
  })

  it('applies the user rate to every token price instead of the group default', () => {
    const wrapper = card(model(), {
      ...group,
      rate_multiplier: 2,
      user_rate_multiplier: 0.5,
    })
    expect(wrapper.text()).toContain('$1.50')
    expect(wrapper.text()).toContain('$7.50')
    expect(wrapper.text()).toContain('$0.15')
  })

  it('keeps the image price unit per image and applies the independent image rate', () => {
    const m = model()
    m.pricing = {
      ...m.pricing!,
      billing_mode: 'image',
      per_request_price: 0.025,
    }
    const wrapper = card(m, {
      ...group,
      rate_multiplier: 2,
      image_rate_independent: true,
      image_rate_multiplier: 3,
    })
    expect(wrapper.text()).toContain('$0.075')
    expect(wrapper.text()).toContain('modelPlaza.table.perImage')
    expect(wrapper.text()).not.toContain('$75000')
  })

  it('resolves configured tier multipliers while leaving official tiers out', () => {
    const m = model()
    m.pricing!.intervals = [
      {
        min_tokens: 200000,
        max_tokens: null,
        input_price: null,
        output_price: null,
        input_multiplier: 2,
        output_multiplier: 1.5,
        cache_read_price: null,
        cache_write_price: null,
        per_request_price: null,
      },
    ]
    m.official_pricing!.intervals = [
      {
        min_tokens: 999999,
        max_tokens: null,
        input_price: 1,
        output_price: 1,
        cache_read_price: null,
        cache_write_price: null,
        per_request_price: null,
      },
    ]
    const wrapper = card(m)
    expect(wrapper.find('details').exists()).toBe(true)
    expect(wrapper.text()).toContain('>200K')
    expect(wrapper.text()).toContain('$22.50')
    expect(wrapper.text()).not.toContain('999.999K')
    expect(wrapper.text()).not.toContain('$1000000')
  })

  it('does not substitute official prices when site pricing is missing', () => {
    const wrapper = card(model({ pricing: null }))
    expect(wrapper.text()).toContain('modelPlaza.detail.noPricing')
    expect(wrapper.text()).not.toContain('$')
  })

  it('includes configured image output token prices', () => {
    const m = model({ name: 'gemini-3.1-flash-image' })
    m.pricing!.image_output_price = 0.00006
    const wrapper = card(m)
    expect(wrapper.text()).toContain('Google Gemini')
    expect(wrapper.text()).toContain('modelPlaza.cards.imageOutput')
    expect(wrapper.text()).toContain('$60.00')
  })

  it('presents image generation as one tool with an endpoint and actual image price', () => {
    const m = model({ name: 'image_generation', tool_name: 'image_generation', endpoint: '/v1/tools/image_generation', official_pricing: null })
    m.pricing = { ...m.pricing!, billing_mode: 'image', per_request_price: 0.15 }
    const wrapper = card(m)
    expect(wrapper.text()).toContain('modelPlaza.cards.imageGeneration')
    expect(wrapper.text()).toContain('modelPlaza.cards.otherModels')
    expect(wrapper.text()).toContain('POST /v1/tools/image_generation')
    expect(wrapper.text()).toContain('$0.15')
    expect(wrapper.text()).not.toContain('gpt-image-')
  })
  it('lists three tool models and updates the displayed price and image tiers', async () => {
    const base = model({ name: 'image_generation', tool_name: 'image_generation' })
    const priced = (value: number) => ({ ...base.pricing!, billing_mode: 'image' as const, per_request_price: value, intervals: [] })
    base.pricing = priced(0.15)
    base.tool_models = [
      { name: 'gpt-image-2.5-flare', pricing: priced(0.15) },
      { name: 'gpt-image-2.5-sunburst', pricing: priced(0.20) },
      { name: 'gpt-image-2', pricing: priced(0.10) },
    ]
    const wrapper = card(base)
    for (const name of base.tool_models.map(x => x.name)) expect(wrapper.text()).toContain(name)
    await wrapper.findAll('button').find(button => button.find('span').text() === 'gpt-image-2')!.trigger('click')
    expect(wrapper.find('.plaza-main-prices').text()).toContain('$0.10')
  })
  it('shows the exact small fixed recognition price', () => {
    const m = model({ name: 'image_recognition', tool_name: 'image_recognition' })
    m.pricing = { ...m.pricing!, billing_mode: 'image', per_request_price: 0.002, intervals: [] }
    expect(card(m).find('.plaza-main-prices').text()).toContain('$0.002')
  })

})
