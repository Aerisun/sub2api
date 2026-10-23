/** Model brands for public presentation, independent of the gateway transport. */
interface ModelBrand {
  provider: string
  label: string
  color: string
  darkColor: string
  secondaryColor: string
  darkSecondaryColor: string
}

const brands: [string, ModelBrand][] = [
  ['gpt-', { provider: 'openai', label: 'OpenAI', color: '#168373', darkColor: '#72dbbc', secondaryColor: '#387c94', darkSecondaryColor: '#8ecddb' }],
  [
    'claude-',
    { provider: 'anthropic', label: 'Anthropic', color: '#a65335', darkColor: '#edb28f', secondaryColor: '#bd5279', darkSecondaryColor: '#ff91b0' },
  ],
  [
    'gemini-',
    { provider: 'gemini', label: 'Google Gemini', color: '#4169c1', darkColor: '#8fbcff', secondaryColor: '#198399', darkSecondaryColor: '#65e2eb' },
  ],
  [
    'deepseek-',
    { provider: 'deepseek', label: 'DeepSeek', color: '#2468c6', darkColor: '#7faaff', secondaryColor: '#6b5ed1', darkSecondaryColor: '#b1a0ff' },
  ],
  ['glm-', { provider: 'zhipu', label: 'GLM', color: '#7054b8', darkColor: '#b4a0ff', secondaryColor: '#b34d97', darkSecondaryColor: '#f2a2dc' }],
  ['kimi-', { provider: 'kimi', label: 'Kimi', color: '#087c66', darkColor: '#62ddb7', secondaryColor: '#2479a5', darkSecondaryColor: '#8bd9ff' }],
  ['qwen', { provider: 'qwen', label: 'Qwen', color: '#8153b5', darkColor: '#c5a0ff', secondaryColor: '#4b69b7', darkSecondaryColor: '#96b9ff' }],
  ['nemotron-', { provider: 'nvidia', label: 'NVIDIA', color: '#4b7415', darkColor: '#a4d85f', secondaryColor: '#158273', darkSecondaryColor: '#70dcc7' }],
  ['step-', { provider: 'stepfun', label: '阶跃星辰', color: '#2563b8', darkColor: '#8bb8ff', secondaryColor: '#486fb1', darkSecondaryColor: '#a0bcf0' }],
  ['minimax-', { provider: 'minimax', label: 'MiniMax', color: '#ba416c', darkColor: '#ffa1bd', secondaryColor: '#b67820', darkSecondaryColor: '#ffd08c' }],
]
const other: ModelBrand = { provider: '', label: '', color: '#0f766e', darkColor: '#5eead4', secondaryColor: '#316fa4', darkSecondaryColor: '#8bcfff' }

export function modelBrand(model: string): ModelBrand {
  const name = model.toLowerCase()
  return brands.find(([prefix]) => name.startsWith(prefix))?.[1] ?? other
}
