<template>
  <div class="bg-gray-50 dark:bg-dark-950" :class="fullHeight ? 'h-dvh overflow-hidden' : 'min-h-screen'">
    <!-- Background Decoration -->
    <div class="pointer-events-none fixed inset-0 bg-mesh-gradient"></div>

    <!-- Sidebar -->
    <AppSidebar />

    <!-- Main Content Area -->
    <div
      class="relative transition-all duration-300"
      :class="[sidebarCollapsed ? 'lg:ml-[72px]' : 'lg:ml-64', fullHeight ? 'flex h-full min-h-0 flex-col overflow-hidden' : 'min-h-screen']"
    >
      <!-- Header -->
      <AppHeader :class="fullHeight ? 'shrink-0' : undefined" />

      <!-- Main Content -->
      <main :class="contentClass">
        <slot />
      </main>
    </div>
  </div>
</template>

<script setup lang="ts">
import '@/styles/onboarding.css'
import { computed, onMounted } from 'vue'
import { useAppStore } from '@/stores'
import { useAuthStore } from '@/stores/auth'
import { useOnboardingTour } from '@/composables/useOnboardingTour'
import { useOnboardingStore } from '@/stores/onboarding'
import AppSidebar from './AppSidebar.vue'
import AppHeader from './AppHeader.vue'

withDefaults(defineProps<{ contentClass?: string; fullHeight?: boolean }>(), {
  contentClass: 'p-4 md:p-6 lg:p-8',
  fullHeight: false
})

const appStore = useAppStore()
const authStore = useAuthStore()
const sidebarCollapsed = computed(() => appStore.sidebarCollapsed)
const isAdmin = computed(() => authStore.user?.role === 'admin')

const { replayTour } = useOnboardingTour({
  storageKey: isAdmin.value ? 'admin_guide' : 'user_guide',
  autoStart: true
})

const onboardingStore = useOnboardingStore()

onMounted(() => {
  onboardingStore.setReplayCallback(replayTour)
})

defineExpose({ replayTour })
</script>
