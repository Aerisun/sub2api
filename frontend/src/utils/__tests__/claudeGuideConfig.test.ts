import { readFileSync } from 'node:fs'
import { beforeAll, describe, expect, it } from 'vitest'

const models = ['kimi-k3', 'glm-5.3', 'glm-5.3-flash', 'deepseek-v4.1-flash', 'Nemotron-3.5', 'step-5-preview']

beforeAll(() => {
  localStorage.clear()
  document.documentElement.innerHTML = readFileSync('public/api-guide.html', 'utf8')
  window.eval(readFileSync('public/api-guide-builder.js', 'utf8'))
  document.dispatchEvent(new Event('DOMContentLoaded'))
})

describe('Claude guide client configurations', () => {
  for (const model of models) {
    for (const effort of ['', 'low', 'medium', 'high', 'xhigh', 'max', 'auto']) {
      it(`exports valid CLI and extension controls for ${model} ${effort || 'default'}`, () => {
        const modelSelect = document.querySelector<HTMLSelectElement>('#guide-model')!
        const effortSelect = document.querySelector<HTMLSelectElement>('#guide-effort')!
        modelSelect.value = model
        effortSelect.value = effort
        modelSelect.dispatchEvent(new Event('change', { bubbles: true }))
        const read = (id: string) => JSON.parse(document.getElementById(id)!.textContent!)
        const shared = read('vscode-shared-code')
        const cli = read('claude-settings-code')
        const extension = read('vscode-code')
        const env = Object.fromEntries(extension['claudeCode.environmentVariables']
          .map((entry: { name: string, value: string }) => [entry.name, entry.value]))
        expect(cli).toMatchObject(shared)
        expect(shared.model).toBe(model)
        expect(shared.autoCompactEnabled).toBe(true)
        expect(shared.autoCompactWindow).toBeUndefined()
        expect(shared.env.CLAUDE_CODE_AUTO_COMPACT_WINDOW).toBe('')
        expect(env.CLAUDE_CODE_AUTO_COMPACT_WINDOW).toBe('')
        expect(env.DISABLE_AUTO_COMPACT).toBe('')
        expect(env.DISABLE_COMPACT).toBe('')
        expect(shared.env.ANTHROPIC_MODEL).toBe(model)
        expect(env.ANTHROPIC_MODEL).toBe(model)
        expect(env.ANTHROPIC_AUTH_TOKEN).toBe(shared.env.ANTHROPIC_AUTH_TOKEN)
        expect(env.ANTHROPIC_BASE_URL).toBe('https://aerisun.top/sub2api')
        expect(env.ANTHROPIC_API_KEY).toBe('')
        expect(env.CLAUDE_CODE_OAUTH_TOKEN).toBe('')
        // The real Claude parser silently ignores effortLevel=max.
        const saved = ['low', 'medium', 'high', 'xhigh'].includes(effort) ? effort : 'low'
        expect(shared.modelSettings[model]).toEqual({ effortLevel: saved, maxEffortLevel: 'max', autoCompactWindow: 1000000 })
        expect(shared.env.CLAUDE_CODE_EFFORT_LEVEL).toBe('')
        expect(shared.env.AERISUN_CLAUDE_INITIAL_EFFORT).toBe(effort === 'max' ? 'max' : '')
        expect(env.CLAUDE_CODE_EFFORT_LEVEL).toBe(shared.env.CLAUDE_CODE_EFFORT_LEVEL)
        for (const id of models) {
          expect(shared.modelPicker.options.find((option: { model: string }) => option.model === id))
            .toMatchObject({ model: id, behavesAs: 'claude-sonnet-5' })
          expect(shared.modelPicker.options.some((option: { model: string }) => option.model === `${id}-max`)).toBe(false)
        }
        const selected = ['low', 'medium', 'high', 'xhigh', 'max'].includes(effort) ? effort : 'low'
        expect(document.getElementById('claude-code')!.textContent).toContain(`claude --effort ${selected}`)
      })
    }
  }
})
