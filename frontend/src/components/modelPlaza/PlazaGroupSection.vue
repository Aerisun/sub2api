<template>
  <section class="space-y-4">
    <p v-if="peakNote" class="text-xs text-amber-600 dark:text-amber-400">
      {{ peakNote }}
    </p>
    <div class="grid grid-cols-1 items-stretch gap-4 sm:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-4">
      <PlazaModelCard
        v-for="model in group.models"
        :key="model.name"
        :model="model"
        :group="group"
      />
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import PlazaModelCard from './PlazaModelCard.vue'
import type { ModelPlazaGroup } from '@/api/modelPlaza'
import { hasPeakRate, formatPeakRateWindow, serverTimezoneLabel } from '@/utils/peak-rate'
import { useAppStore } from '@/stores/app'

const props = defineProps<{ group: ModelPlazaGroup }>()
const { t } = useI18n()
const appStore = useAppStore()
const peakNote = computed(() =>
  hasPeakRate(props.group)
    ? t('modelPlaza.detail.peakNote', {
        window: formatPeakRateWindow(
          props.group,
          serverTimezoneLabel(appStore.cachedPublicSettings?.server_utc_offset),
        ),
        multiplier: props.group.peak_rate_multiplier,
      })
    : '',
)
</script>
