<template>
  <fieldset class="space-y-3" data-test="model-access-selector">
    <legend class="input-label">{{ t('keys.modelAccess') }}</legend>
    <div class="flex flex-wrap gap-x-5 gap-y-2 text-sm">
      <label class="flex cursor-pointer items-center gap-2">
        <input type="radio" name="model-access" :checked="!restricted" @change="$emit('update:restricted', false)" />
        {{ t('keys.allModels') }}
      </label>
      <label class="flex cursor-pointer items-center gap-2">
        <input type="radio" name="model-access" :checked="restricted" @change="$emit('update:restricted', true)" />
        {{ t('keys.specificModels') }}
      </label>
    </div>
    <p class="text-xs text-gray-500 dark:text-gray-400">{{ t('keys.modelAccessHint') }}</p>
    <div v-if="restricted" class="space-y-2 rounded-lg border border-gray-200 p-3 dark:border-dark-600">
      <input v-model="search" type="search" class="input" :placeholder="t('keys.searchModels')" :aria-label="t('keys.searchModels')" />
      <div class="flex flex-wrap items-center justify-between gap-2 text-xs">
        <span class="text-gray-500 dark:text-gray-400">{{ t('keys.modelsSelected', { count: models.length }) }}</span>
        <div class="flex gap-3">
          <button type="button" class="text-primary-600 dark:text-primary-400" :disabled="!available.length" @click="$emit('update:models', [...available])">{{ t('keys.selectAllModels') }}</button>
          <button type="button" class="text-gray-500 dark:text-gray-400" @click="$emit('update:models', [])">{{ t('keys.clearModels') }}</button>
        </div>
      </div>
      <p v-if="loading" class="py-3 text-sm text-gray-500">{{ t('common.loading') }}</p>
      <div v-else-if="failed && !available.length" role="alert" class="text-sm text-red-500">
        {{ t('keys.failedToLoadModels') }}
        <button type="button" class="ml-2 underline" @click="loadModels">{{ t('common.refresh') }}</button>
      </div>
      <div v-else class="max-h-48 space-y-1 overflow-y-auto">
        <label v-for="model in filteredModels" :key="model" class="flex cursor-pointer items-start gap-2 rounded px-2 py-1.5 text-sm hover:bg-gray-50 dark:hover:bg-dark-700">
          <input type="checkbox" class="mt-0.5 shrink-0" :checked="models.includes(model)" @change="toggle(model)" />
          <span class="min-w-0 break-all text-gray-700 dark:text-gray-200">{{ model.startsWith('gpt-image-') ? t('modelPlaza.cards.legacyImagePermission') : model }}<span v-if="!available.includes(model)" class="ml-2 text-xs text-amber-600">{{ t('keys.modelUnavailable') }}</span></span>
        </label>
        <p v-if="!filteredModels.length" class="py-3 text-sm text-gray-500">{{ t('keys.noModelsFound') }}</p>
      </div>
    </div>
  </fieldset>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { modelPlazaAPI, type ModelPlazaGroup } from '@/api/modelPlaza'
import type { ModelAllowlist } from '@/types'

const props = defineProps<{ groupId: number | null; restricted: boolean; models: string[]; allowlist?: ModelAllowlist }>()
const emit = defineEmits<{ 'update:restricted': [value: boolean]; 'update:models': [value: string[]] }>()
const { t } = useI18n()
const catalog = ref<ModelPlazaGroup[]>([])
const search = ref('')
const loading = ref(false)
const failed = ref(false)
const available = computed(() => {
  const catalogModels = catalog.value.find(group => group.id === props.groupId)?.models.map(model => model.name) ?? []
  if (!props.allowlist?.enabled) return [...new Set(catalogModels)].sort()
  const entries = props.allowlist.models ?? []
  const exact = entries.filter(model => !model.endsWith('*') && !model.startsWith('gpt-image-'))
  const expanded = catalogModels.filter(model => entries.some(entry => entry.endsWith('*')
    ? model.toLowerCase().startsWith(entry.slice(0, -1).toLowerCase())
    : model.toLowerCase() === entry.toLowerCase()))
  return [...new Set([...exact, ...expanded])].sort()
})
const filteredModels = computed(() => [...new Set([...available.value, ...props.models])].filter(model => model.toLowerCase().includes(search.value.toLowerCase())).sort())

function toggle(model: string) {
  emit('update:models', props.models.includes(model) ? props.models.filter(value => value !== model) : [...props.models, model])
}
async function loadModels() {
  loading.value = true
  failed.value = false
  try {
    catalog.value = (await modelPlazaAPI.getModelPlaza()).groups
  } catch {
    failed.value = true
  } finally {
    loading.value = false
  }
}
onMounted(loadModels)
</script>
