<template>
  <AppLayout content-class="chat-page-content" full-height>
    <iframe ref="chatFrame" :src="chatUrl" title="对话聊天" class="chat-frame" allow="clipboard-write" />
  </AppLayout>
</template>
<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'
import AppLayout from '@/components/layout/AppLayout.vue'
import { useAuthStore } from '@/stores/auth'
const chatUrl = `${import.meta.env.BASE_URL}chat-ui/index.html`
const chatFrame = ref<HTMLIFrameElement | null>(null)
const auth = useAuthStore()
function onChatUsage(event: MessageEvent) {
  if (event.origin !== window.location.origin || event.source !== chatFrame.value?.contentWindow) return
  if (event.data?.type === 'chat:usage-updated') void auth.refreshUser().catch(() => {})
}
onMounted(() => {
  document.body.classList.add('chat-page-active')
  window.addEventListener('message', onChatUsage)
})
onUnmounted(() => {
  document.body.classList.remove('chat-page-active')
  window.removeEventListener('message', onChatUsage)
})
</script>
<style scoped>
:global(body.chat-page-active) {
  height: 100dvh;
  min-height: 0;
  overflow: hidden;
  overscroll-behavior: none;
}
:global(.chat-page-content) {
  padding: 0;
  flex: 1;
  height: auto;
  min-height: 0;
  overflow: hidden;
}
.chat-frame {
  display: block;
  width: 100%;
  height: 100%;
  border: 0;
  border-radius: 0;
}
</style>
