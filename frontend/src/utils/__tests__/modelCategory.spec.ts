import { describe, expect, it } from 'vitest'
import { isOtherModel, toolLabelKey } from '../modelCategory'

describe('plaza capability categories', () => {
  it('groups tools, embeddings and rerank together without moving chat models', () => {
    for (const name of ['image_generation', 'image_recognition', 'web_search', 'qwen3-embedding-4b', 'qwen3-reranker-4b'])
      expect(isOtherModel({ name, pricing: null })).toBe(true)
    expect(isOtherModel({ name: 'glm-5.3', pricing: null })).toBe(false)
    expect(toolLabelKey('image_generation')).toBe('imageGeneration')
  })
})
