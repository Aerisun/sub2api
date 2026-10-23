import type { PlazaModel } from '@/api/modelPlaza'

export function isOtherModel(model: Pick<PlazaModel, 'name' | 'pricing' | 'tool_name'>): boolean {
  return !!model.tool_name || /embedding|rerank|^image_|^web_search$|^gpt-image-/i.test(model.name) || model.pricing?.billing_mode === 'image'
}

export function toolLabelKey(name: string): string | undefined {
  return ({ image_generation: 'imageGeneration', image_recognition: 'imageRecognition', web_search: 'webSearch' } as Record<string,string>)[name]
}
