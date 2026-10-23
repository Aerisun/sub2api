<template>
  <div class="mx-auto w-full max-w-[1600px] space-y-6">
    <!-- 页头(独立形态下展示标题;后台形态 AppHeader 已有页面标题) -->
    <div v-if="!embedded">
      <h1 class="text-2xl font-bold tracking-tight text-gray-900 dark:text-white sm:text-3xl">
        {{ t('modelPlaza.title') }}
      </h1>
      <p class="mt-1.5 text-sm text-gray-500 dark:text-dark-400">
        {{ t('modelPlaza.description') }}
      </p>
    </div>

    <!-- 全局价格说明(管理员配置,Markdown) -->
    <div
      v-if="descriptionHtml"
      class="plaza-description rounded-2xl border border-gray-100 bg-white px-5 py-4 text-sm shadow-card dark:border-dark-700/50 dark:bg-dark-800/50"
      v-html="descriptionHtml"
    ></div>

    <!-- 未登录提示 -->
    <p
      v-if="!isAuthenticated"
      class="flex items-center gap-1.5 text-xs text-gray-400 dark:text-dark-500"
    >
      <Icon name="infoCircle" size="xs" class="h-3.5 w-3.5" />
      {{ t('modelPlaza.anonymousHint') }}
    </p>

    <!-- 加载/错误/空 -->
    <div v-if="loading" class="flex min-h-[240px] items-center justify-center">
      <div
        class="h-8 w-8 animate-spin rounded-full border-2 border-primary-600/25 border-t-primary-600 dark:border-primary-400/25 dark:border-t-primary-400"
      ></div>
    </div>
    <div
      v-else-if="error"
      class="rounded-2xl border border-red-200 bg-red-50 px-5 py-8 text-center text-sm text-red-600 dark:border-red-500/30 dark:bg-red-500/10 dark:text-red-300"
    >
      {{ t('modelPlaza.loadFailed') }}
    </div>
    <template v-else>
      <div class="flex flex-col gap-4 lg:flex-row lg:items-center lg:justify-between">
        <div
          class="flex min-w-0 flex-wrap gap-2"
          role="group"
          :aria-label="t('modelPlaza.cards.brandFilter')"
        >
          <button
            type="button"
            class="brand-filter inline-flex h-10 items-center gap-2 rounded-lg border px-3 text-sm font-medium transition-colors focus-visible:outline-primary-500"
            :aria-pressed="selectedBrand === 'all'"
            @click="selectedBrand = 'all'"
          >
            {{ t('modelPlaza.filters.all') }}
          </button>
          <button
            v-for="brand in brands"
            :key="brand.provider"
            :style="{ '--model-accent-light': brand.color, '--model-accent-dark': brand.darkColor }"
            type="button"
            class="brand-filter inline-flex h-10 items-center gap-2 rounded-lg border px-3 text-sm font-medium transition-colors focus-visible:outline-primary-500"
            :aria-pressed="selectedBrand === brand.provider"
            @click="selectedBrand = brand.provider"
          >
            <span class="h-1.5 w-1.5 rounded-full bg-current" aria-hidden="true"></span>
            {{ brand.label }}
          </button>
        </div>
        <SearchInput
          v-model="searchQuery"
          :placeholder="t('modelPlaza.filters.searchPlaceholder')"
          class="plaza-search min-w-0 w-full lg:w-72 lg:flex-shrink-0"
        />
      </div>

      <!-- 模型卡片仅使用站点实际定价。 -->
      <div v-if="filteredGroups.length > 0" class="space-y-5">
        <PlazaGroupSection v-for="g in filteredGroups" :key="g.id" :group="g" />
      </div>
      <div
        v-else
        class="rounded-2xl border border-dashed border-gray-300 px-5 py-12 text-center text-sm text-gray-500 dark:border-dark-600 dark:text-dark-400"
      >
        {{ searchActive ? t('modelPlaza.noSearchResult') : t('modelPlaza.empty') }}
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { marked } from 'marked'
import DOMPurify from 'dompurify'
import Icon from '@/components/icons/Icon.vue'
import SearchInput from '@/components/common/SearchInput.vue'
import { modelBrand } from '@/utils/modelBrand'
import { isOtherModel, toolLabelKey } from '@/utils/modelCategory'
import PlazaGroupSection from './PlazaGroupSection.vue'
import type { ModelPlazaGroup, ModelPlazaResponse } from '@/api/modelPlaza'
import { useAuthStore } from '@/stores/auth'

const props = defineProps<{
  response: ModelPlazaResponse | null
  loading: boolean
  error?: boolean
  /** 后台内嵌形态(AppLayout 内):隐藏页头。 */
  embedded?: boolean
}>()

const { t } = useI18n()
const authStore = useAuthStore()
const isAuthenticated = computed(() => authStore.isAuthenticated)

const selectedBrand = ref('all')
const searchQuery = ref('')

const searchActive = computed(
  () => searchQuery.value.trim() !== '' || selectedBrand.value !== 'all',
)

const descriptionHtml = computed(() => {
  const md = props.response?.description?.trim()
  if (!md) return ''
  return DOMPurify.sanitize(marked.parse(md) as string)
})

/** 生效倍率 = 用户专属倍率 ?? 分组默认倍率。 */
function effectiveRate(g: ModelPlazaGroup): number {
  return g.user_rate_multiplier ?? g.rate_multiplier
}

const brandOrder = ['openai', 'anthropic', 'gemini', 'deepseek', 'zhipu', 'kimi', 'qwen', 'nvidia', 'stepfun', 'minimax', 'other']
function plazaBrand(model: ModelPlazaGroup['models'][number]) {
  return isOtherModel(model) ? { ...modelBrand(''), provider:'other', label:t('modelPlaza.cards.otherModels') } : modelBrand(model.name)
}
const brands = computed(() => {
  const found = new Map<string, ReturnType<typeof modelBrand>>()
  for (const group of props.response?.groups ?? []) {
    for (const model of group.models) {
      const brand = plazaBrand(model)
      if (brand.provider) found.set(brand.provider, brand)
    }
  }
  return [...found.values()].sort(
    (a, b) => brandOrder.indexOf(a.provider) - brandOrder.indexOf(b.provider),
  )
})
watch(brands, (list) => {
  if (
    selectedBrand.value !== 'all' &&
    !list.some((brand) => brand.provider === selectedBrand.value)
  )
    selectedBrand.value = 'all'
})

const filteredGroups = computed(() => {
  const query = searchQuery.value.trim().toLowerCase()
  return (props.response?.groups ?? [])
    .map((group) => ({
      ...group,
      models: group.models
        .filter(
          (model) =>
            (selectedBrand.value === 'all' ||
              plazaBrand(model).provider === selectedBrand.value) &&
            (!query || model.name.toLowerCase().includes(query) ||
              (model.tool_name && t(`modelPlaza.cards.${toolLabelKey(model.tool_name)}`).toLowerCase().includes(query))),
        )
        .sort(
          (a, b) =>
            brandOrder.indexOf(plazaBrand(a).provider) -
              brandOrder.indexOf(plazaBrand(b).provider) || a.name.localeCompare(b.name),
        ),
    }))
    .filter((group) => group.models.length > 0)
    .sort((a, b) => effectiveRate(a) - effectiveRate(b) || a.name.localeCompare(b.name))
})
</script>

<style scoped>
.plaza-search :deep(input) { height: 40px; }

.brand-filter {
  --model-accent: var(--model-accent-light, #0f766e);
  color: var(--model-accent);
  background: color-mix(in srgb, var(--model-accent) 5%, #fff);
  border-color: color-mix(in srgb, var(--model-accent) 12%, #e5e7eb);
}
.dark .brand-filter {
  --model-accent: var(--model-accent-dark, #5eead4);
  background: color-mix(in srgb, var(--model-accent) 6%, #152232);
  border-color: color-mix(in srgb, var(--model-accent) 15%, #26384d);
}
.brand-filter:hover,
.brand-filter[aria-pressed='true'] {
  background: color-mix(in srgb, var(--model-accent) 15%, #fff);
  border-color: color-mix(in srgb, var(--model-accent) 50%, #e5e7eb);
}
.dark .brand-filter:hover,
.dark .brand-filter[aria-pressed='true'] {
  background: color-mix(in srgb, var(--model-accent) 18%, #152232);
  border-color: color-mix(in srgb, var(--model-accent) 55%, #26384d);
}

.plaza-description {
  line-height: 1.7;
  overflow-wrap: anywhere;
}

.plaza-description :deep(h1),
.plaza-description :deep(h2),
.plaza-description :deep(h3) {
  @apply mb-2 mt-3 font-semibold text-gray-900 first:mt-0 dark:text-white;
}

.plaza-description :deep(p) {
  @apply mb-2 text-gray-700 last:mb-0 dark:text-dark-200;
}

.plaza-description :deep(a) {
  @apply text-primary-600 underline underline-offset-4 hover:text-primary-700 dark:text-primary-300;
}

.plaza-description :deep(ul) {
  @apply mb-2 list-disc pl-5;
}

.plaza-description :deep(ol) {
  @apply mb-2 list-decimal pl-5;
}

.plaza-description :deep(li) {
  @apply mb-0.5 text-gray-700 dark:text-dark-200;
}

.plaza-description :deep(code) {
  @apply rounded bg-gray-100 px-1.5 py-0.5 font-mono text-xs dark:bg-dark-800;
}

.plaza-description :deep(blockquote) {
  @apply my-2 border-l-4 border-gray-300 pl-3 text-gray-600 dark:border-dark-600 dark:text-dark-300;
}
</style>
