<template>
  <article
    class="plaza-model-card flex h-full min-w-0 flex-col rounded-2xl border p-5 transition-colors"
    :style="{
      '--model-accent-light': brand.color,
      '--model-accent-dark': brand.darkColor,
      '--model-secondary-light': brand.secondaryColor,
      '--model-secondary-dark': brand.darkSecondaryColor
    }"
    data-testid="plaza-model-card"
  >
    <div class="flex items-center justify-between gap-3">
      <div class="flex items-center gap-2.5 text-sm font-medium text-gray-600 dark:text-dark-300">
        <span class="plaza-brand-icon flex h-9 w-9 items-center justify-center rounded-xl">
          <Icon v-if="toolKey" :name="toolIcon" size="md" />
          <ProviderIcon v-else :provider="brand.provider" :size="23" />
        </span>
        {{ brand.label || t('keys.providers.other') }}
      </div>
      <span class="plaza-brand-badge rounded-md px-2 py-1 text-xs">{{ group.name }}</span>
    </div>
    <h3
      class="mt-4 min-h-[3rem] break-words font-mono text-base font-semibold leading-6 text-gray-900 dark:text-white"
    >
      {{ toolKey ? t('modelPlaza.cards.' + toolKey) : model.name }}
    </h3>
    <p v-if="toolKey" class="-mt-2 mb-2 break-all font-mono text-xs text-gray-500 dark:text-dark-300">{{ model.name }}</p>
    <p v-if="model.endpoint" class="mb-2 break-all text-xs text-gray-500 dark:text-dark-300">POST {{ model.endpoint }}</p>
    <p v-if="['claude-sonnet-4-6', 'claude-opus-4-6-thinking', 'gemini-3.8-flash'].includes(model.name)" class="mb-2 text-xs text-gray-500 dark:text-dark-300">流式 · 思考强度 · 工具调用 · 原生识图</p>
    <div v-if="model.tool_models?.length" class="mb-3 space-y-1" :aria-label="t('modelPlaza.cards.imageModels')">
      <button v-for="option in model.tool_models" :key="option.name" type="button"
        class="flex w-full min-w-0 items-center justify-between gap-2 rounded-md border px-2 py-1.5 text-left text-xs"
        :class="selectedImageModel === option.name ? 'border-primary-500/60 bg-primary-500/10' : 'border-transparent hover:bg-gray-500/10'"
        :aria-pressed="selectedImageModel === option.name" @click="selectedImageModel = option.name">
        <span class="break-all font-mono">{{ option.name }}</span><span class="shrink-0 font-mono">{{ requestPrice(option.pricing?.per_request_price) }}</span>
      </button>
    </div>
    <p class="plaza-unit mt-1 text-xs text-gray-400 dark:text-dark-300">{{ isToken ? unit : 'USD' }}</p>
    <dl v-if="pricing" class="plaza-main-prices mt-3 grid gap-4" :class="isToken ? 'grid-cols-2' : 'grid-cols-1'">
      <template v-if="isToken">
        <div>
          <dt class="text-xs text-gray-500 dark:text-dark-300">
            {{ t('modelPlaza.table.input') }}
          </dt>
          <dd
            class="mt-1 font-mono text-2xl font-semibold tabular-nums tracking-tight text-gray-900 dark:text-white"
          >
            {{ tokenPrice(pricing.input_price) }}
          </dd>
        </div>
        <div>
          <dt class="text-xs text-gray-500 dark:text-dark-300">
            {{ t('modelPlaza.table.output') }}
          </dt>
          <dd class="plaza-price mt-1 font-mono text-2xl font-semibold tabular-nums tracking-tight">
            {{ tokenPrice(pricing.output_price) }}
          </dd>
        </div>
      </template>
      <div v-else>
        <dt class="text-xs text-gray-500 dark:text-dark-300">{{ unit }}</dt>
        <dd class="plaza-price mt-1 font-mono text-2xl font-semibold tabular-nums tracking-tight">
          {{ requestPrice(pricing.per_request_price) }}
        </dd>
      </div>
    </dl>
    <p v-else class="mt-3 text-sm text-gray-400">
      {{ t('modelPlaza.detail.noPricing') }}
    </p>
    <div class="plaza-extra-area">
      <dl
        v-if="extras.length"
        class="space-y-2 border-t border-gray-100 pt-4 text-xs dark:border-dark-700"
      >
        <div
          v-for="extra in extras"
          :key="extra.label"
          class="flex items-center justify-between gap-3"
        >
          <dt class="text-gray-500 dark:text-dark-300">{{ extra.label }}</dt>
          <dd class="shrink-0 font-mono font-medium tabular-nums text-gray-700 dark:text-dark-200">
            {{ tokenPrice(extra.price) }}
          </dd>
        </div>
      </dl>
    </div>
    <details
      v-if="hasDetails"
      class="mt-5 border-t border-gray-100 pt-4 text-xs dark:border-dark-700"
    >
      <summary class="plaza-price cursor-pointer focus-visible:outline-primary-500">
        {{ t('modelPlaza.cards.morePricing') }}
      </summary>
      <div class="mt-3 space-y-3 text-gray-500 dark:text-dark-300">
        <template v-if="intervals.length">
          <p v-if="isToken">
            {{
              t(
                model.long_context_basis === 'marginal'
                  ? 'modelPlaza.table.tierHintMarginal'
                  : 'modelPlaza.table.tierHint',
              )
            }}
          </p>
          <div
            v-for="interval in intervals"
            :key="interval.min_tokens"
            class="rounded-lg bg-gray-50 p-3 dark:bg-dark-900/50"
          >
            <p class="mb-2 font-medium text-gray-700 dark:text-dark-200">
              {{ intervalLabel(interval) }}
            </p>
            <dl class="space-y-1.5">
              <template v-if="isToken">
                <div
                  v-for="field in tierFields"
                  :key="field.key"
                  class="flex justify-between gap-3"
                >
                  <dt>{{ field.label }}</dt>
                  <dd class="font-mono tabular-nums text-gray-700 dark:text-dark-200">
                    {{ tokenPrice(interval[field.key]) }}
                  </dd>
                </div>
              </template>
              <div v-else class="flex justify-between gap-3">
                <dt>{{ unit }}</dt>
                <dd class="font-mono tabular-nums">
                  {{ requestPrice(interval.per_request_price) }}
                </dd>
              </div>
            </dl>
          </div>
        </template>
        <template v-if="model.time_pricing?.periods.length">
          <p>
            {{ model.time_pricing.timezone
            }}<span v-if="model.time_pricing.weekdays_only">
              · {{ t('modelPlaza.table.timePricingWeekdays') }}</span
            >
          </p>
          <div
            v-for="period in model.time_pricing.periods"
            :key="period.start_time"
            class="flex justify-between gap-3"
          >
            <span>{{ period.start_time }}–{{ period.end_time }}</span>
            <span class="font-mono">×{{ period.multiplier }}</span>
          </div>
        </template>
        <p
          v-for="[effort, multiplier] in reasoningMultipliers"
          :key="effort"
          :title="
            t('modelPlaza.table.reasoningMultiplierHint', {
              effort,
              multiplier,
            })
          "
        >
          {{
            t('modelPlaza.table.reasoningMultiplierBadge', {
              effort,
              multiplier,
            })
          }}
        </p>
      </div>
    </details>
  </article>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import ProviderIcon from '@/components/user/monitor/ProviderIcon.vue'
import Icon from '@/components/icons/Icon.vue'
import type { ModelPlazaGroup, PlazaModel } from '@/api/modelPlaza'
import type { UserPricingInterval } from '@/api/channels'
import { modelBrand } from '@/utils/modelBrand'
import { isOtherModel, toolLabelKey } from '@/utils/modelCategory'
import { formatScaled, resolveIntervalPrices } from '@/utils/pricing'

const props = defineProps<{ model: PlazaModel; group: ModelPlazaGroup }>()
const { t } = useI18n()
const brand = computed(() => isOtherModel(props.model) ? {...modelBrand(props.model.name),label:t('modelPlaza.cards.otherModels')} : modelBrand(props.model.name))
const toolKey = computed(() => toolLabelKey(props.model.tool_name || props.model.name))
const toolIcon = computed(() => props.model.name === 'web_search' ? 'globe' : props.model.name === 'image_recognition' ? 'eye' : 'sparkles')
const selectedImageModel = ref('')
watch(() => props.model.tool_models, options => { if (!options?.some(option => option.name === selectedImageModel.value)) selectedImageModel.value = options?.[0]?.name || '' }, { immediate: true })
const pricing = computed(() => props.model.tool_models?.find(option => option.name === selectedImageModel.value)?.pricing ?? props.model.pricing)
const isToken = computed(() => !pricing.value || pricing.value.billing_mode === 'token')
const rate = computed(() => props.group.user_rate_multiplier ?? props.group.rate_multiplier)
const unit = computed(() =>
  isToken.value
    ? t('modelPlaza.cards.tokenUnit')
    : t(
        pricing.value?.billing_mode === 'image'
          ? 'modelPlaza.table.perImage'
          : 'modelPlaza.table.perRequest',
      ),
)

function tokenPrice(value: number | null | undefined): string {
  return formatScaled(value == null ? null : value * rate.value, 1_000_000, 2)
}
function requestPrice(value: number | null | undefined): string {
  const multiplier =
    pricing.value?.billing_mode === 'image' && props.group.image_rate_independent
      ? props.group.image_rate_multiplier
      : rate.value
  return formatScaled(value == null ? null : value * multiplier, 1, 2)
}

type PriceKey =
  | 'input_price'
  | 'output_price'
  | 'cache_write_price'
  | 'cache_write_1h_price'
  | 'cache_read_price'
const extras = computed(() => {
  const p = pricing.value
  if (!p || !isToken.value) return []
  return [
    {
      key: 'cache_read_price' as const,
      label: t('modelPlaza.cards.cacheRead'),
      price: p.cache_read_price,
    },
    {
      key: 'cache_write_price' as const,
      label: t('modelPlaza.cards.cacheWrite'),
      price: p.cache_write_price,
    },
    {
      key: 'cache_write_1h_price' as const,
      label: t('modelPlaza.cards.cacheWrite1h'),
      price: p.cache_write_1h_price,
    },
    {
      key: 'image_input_price' as const,
      label: t('modelPlaza.cards.imageInput'),
      price: p.image_input_price,
    },
    {
      key: 'image_output_price' as const,
      label: t('modelPlaza.cards.imageOutput'),
      price: p.image_output_price,
    },
  ].filter(
    (field) =>
      field.price != null &&
      (field.key !== 'cache_write_1h_price' || p.cache_write_1h_price !== p.cache_write_price),
  )
})
const tierFields = computed<{ key: PriceKey; label: string }[]>(() => [
  { key: 'input_price', label: t('modelPlaza.table.input') },
  { key: 'output_price', label: t('modelPlaza.table.output') },
  ...extras.value
    .filter((extra) => extra.key !== 'image_input_price' && extra.key !== 'image_output_price')
    .map((extra) => ({ key: extra.key as PriceKey, label: extra.label })),
])
const intervals = computed(() => {
  const p = pricing.value
  if (!p) return []
  return [...p.intervals]
    .sort((a, b) => a.min_tokens - b.min_tokens)
    .map((interval) => resolveIntervalPrices(interval, p))
})
const reasoningMultipliers = computed(() =>
  Object.entries(pricing.value?.reasoning_effort_multipliers ?? {}).filter(
    ([, multiplier]) => Number.isFinite(multiplier) && multiplier > 0,
  ),
)
const hasDetails = computed(
  () =>
    intervals.value.length > 0 ||
    (props.model.time_pricing?.periods.length ?? 0) > 0 ||
    reasoningMultipliers.value.length > 0,
)

function intervalLabel(interval: UserPricingInterval): string {
  if (interval.tier_label) return interval.tier_label
  const tokens = (value: number) => (value >= 1000 ? `${value / 1000}K` : String(value))
  return interval.max_tokens == null
    ? `>${tokens(interval.min_tokens)}`
    : `≤${tokens(interval.max_tokens)}`
}
</script>

<style scoped>
.plaza-model-card {
  --model-accent: var(--model-accent-light);
  --model-secondary: var(--model-secondary-light, #316fa4);
  min-height: 332px;
  background-color: #fff;
  background-image:
    radial-gradient(ellipse at top left, color-mix(in srgb, var(--model-accent) 7%, transparent), transparent 65%),
    radial-gradient(ellipse at bottom right, color-mix(in srgb, var(--model-secondary) 4%, transparent), transparent 65%);
  border-color: color-mix(in srgb, var(--model-accent) 15%, #e5e7eb);
  box-shadow: inset 0 2px 0 color-mix(in srgb, var(--model-accent) 45%, transparent), 0 3px 12px rgb(15 23 42 / 3%);
}
.dark .plaza-model-card {
  --model-accent: var(--model-accent-dark);
  --model-secondary: var(--model-secondary-dark, #8bcfff);
  background-color: #131d2e;
  background-image:
    radial-gradient(ellipse at top left, color-mix(in srgb, var(--model-accent) 11%, transparent), transparent 65%),
    radial-gradient(ellipse at bottom right, color-mix(in srgb, var(--model-secondary) 6%, transparent), transparent 65%);
  border-color: color-mix(in srgb, var(--model-accent) 20%, #263346);
  box-shadow: inset 0 2px 0 color-mix(in srgb, var(--model-accent) 55%, transparent), 0 4px 14px rgb(0 0 0 / 8%);
}
.plaza-model-card:hover { border-color: color-mix(in srgb, var(--model-accent) 55%, #94a3b8); }
.plaza-model-card .plaza-brand-icon {
  background: color-mix(in srgb, var(--model-accent) 11%, transparent);
  box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--model-accent) 14%, transparent);
}
.plaza-model-card .plaza-brand-icon span { color: inherit; }
.plaza-model-card .plaza-brand-badge { color: #64748b; background: rgb(148 163 184 / 9%); }
.dark .plaza-model-card .plaza-brand-badge { color: #a6b3c5; background: rgb(148 163 184 / 9%); }
.plaza-main-prices dd { font-size: 1.375rem; line-height: 1.35; white-space: nowrap; }
.plaza-extra-area { margin-top: auto; min-height: 101px; padding-top: 20px; }
.dark .plaza-model-card dt,
.dark .plaza-model-card > p { color: #bac5d4; }

.plaza-brand-icon { color: var(--model-accent); }
.plaza-brand-badge { color: var(--model-accent); }
.plaza-price { color: var(--model-accent); }
</style>
