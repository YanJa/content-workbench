<script setup lang="ts">
/**
 * 声音复刻面板。
 *
 * 交互只有三步：传一段录音 → 等复刻 → 音色出现在上面的选择器里。
 * 刻意不做「训练轮数/步数」这类参数 —— 复刻用的是零样本克隆，
 * 不需要用户理解模型怎么工作，只需要知道"录够 10 秒就能用我的声音念稿"。
 *
 * 为什么单独抽成组件：VideoView 已经 1100 行了，再往里塞录音逻辑会失控。
 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import {
  NAlert,
  NButton,
  NInput,
  NPopconfirm,
  NProgress,
  NTag,
  NUpload,
  useMessage,
  type UploadFileInfo,
} from 'naive-ui'

import {
  cloneVoice,
  deleteCloneVoice,
  getCloneStatus,
  getVoiceProvider,
  renameCloneVoice,
} from '@/api/tools'
import type { CloneVoice, VoiceProviderStatus } from '@/api/tools'
import { getApiBase } from '@/api/config'
import { useAuthStore } from '@/stores/auth'
import { usePermGate } from '@/composables/usePermGate'

/** 复刻出来的音色列表，双向绑定给父组件拼进音色选择器 */
const voices = defineModel<CloneVoice[]>('voices', { required: true })
const emit = defineEmits<{ refresh: [] }>()

const message = useMessage()
const auth = useAuthStore()

/** 声音复刻的门控：权限 voice.clone + 配额 voice_clone */
const cloneGate = usePermGate({ perm: 'voice.clone', quota: 'voice_clone' })

const status = ref<VoiceProviderStatus | null>(null)
const files = ref<UploadFileInfo[]>([])
const nameInput = ref('')
const uploading = ref(false)
const expanded = ref(false)

/* 正在复刻的任务（同一时刻只跑一个，串行更省 CPU） */
const running = ref<{
  id: string
  name: string
  progress: number
  stage: string
} | null>(null)
let timer: number | null = null

const available = computed(() => status.value?.available ?? false)
const isEdgeOnly = computed(() => status.value?.provider === 'edge')

/** 试听地址：后端给的是 /api/… 相对路径，静态通道下要补后端地址 */
function audioUrl(rel: string): string {
  const base = getApiBase()
  return base.startsWith('http') ? base + rel.replace(/^\/api/, '') : rel
}

async function loadStatus() {
  try {
    status.value = await getVoiceProvider()
  } catch {
    status.value = null
  }
}

async function onUpload(options: { file: UploadFileInfo; fileList: UploadFileInfo[] }) {
  const raw = options.file.file
  // 无论成败都清空列表：NUpload max=1，不清掉下次点不动（踩过一次的坑）
  files.value = []
  if (!raw) return
  if (cloneGate.blocked.value) {
    message.warning(cloneGate.reason.value)
    return
  }
  uploading.value = true
  try {
    const res = await cloneVoice(raw, nameInput.value)
    nameInput.value = ''
    message.success('录音已上传，开始复刻音色')
    // 乐观置灰：复刻是异步任务，提交即预扣
    auth.markUsed('voice_clone')
    running.value = { id: res.id, name: res.name, progress: 0, stage: '排队中' }
    pollClone(res.id)
  } catch (e) {
    message.error(`上传失败：${(e as Error).message}`)
  } finally {
    uploading.value = false
  }
}

/** 轮询复刻进度。复刻是短任务（本地首次含下模型可能几分钟），2 秒一探。 */
function pollClone(profileId: string) {
  stopPoll()
  const deadline = Date.now() + 30 * 60 * 1000
  const tick = async () => {
    try {
      const j = await getCloneStatus(profileId)
      if (running.value?.id === profileId) {
        running.value = {
          id: profileId,
          name: running.value.name,
          progress: j.progress,
          stage: j.stage,
        }
      }
      if (j.status === 'done') {
        stopPoll()
        running.value = null
        const q = j.quality?.message ? `（${j.quality.message}）` : ''
        message.success(`音色复刻完成${q}`)
        emit('refresh')
        return
      }
      if (j.status === 'failed') {
        stopPoll()
        running.value = null
        message.error(`复刻失败：${j.error || '未知原因'}`)
        return
      }
      if (Date.now() > deadline) {
        stopPoll()
        running.value = null
        message.warning('复刻超时，请刷新列表看看是否已完成')
        emit('refresh')
      }
    } catch (e) {
      stopPoll()
      running.value = null
      message.error(`查询复刻进度失败：${(e as Error).message}`)
    }
  }
  void tick()
  timer = window.setInterval(tick, 2000)
}

function stopPoll() {
  if (timer !== null) {
    window.clearInterval(timer)
    timer = null
  }
}

async function onRename(v: CloneVoice) {
  const name = window.prompt('给这个音色起个名字', v.name)
  if (!name?.trim() || name.trim() === v.name) return
  try {
    await renameCloneVoice(v.id, name.trim())
    message.success('已改名')
    emit('refresh')
  } catch (e) {
    message.error(`改名失败：${(e as Error).message}`)
  }
}

async function onDelete(v: CloneVoice) {
  try {
    await deleteCloneVoice(v.id)
    message.success('已删除')
    emit('refresh')
  } catch (e) {
    message.error(`删除失败：${(e as Error).message}`)
  }
}

onMounted(loadStatus)
onBeforeUnmount(stopPoll)
</script>

<template>
  <div class="clone-box">
    <div class="clone-head">
      <button type="button" class="clone-toggle" @click="expanded = !expanded">
        <span class="arrow" :class="{ on: expanded }">▸</span>
        录音克隆 · 用自己的声音配音
        <NTag v-if="voices.length" size="tiny" type="success" :bordered="false">
          {{ voices.length }} 个音色
        </NTag>
      </button>
    </div>

    <div v-if="expanded" class="clone-body">
      <NAlert v-if="isEdgeOnly" type="warning" :bordered="false" size="small">
        当前后端没开启声音复刻（<code>VC_PROVIDER=edge</code>）。两条开启方式：
        <br />① <b>本地零成本</b>：后端执行 <code>pip install coqui-tts</code>，再把
        <code>VC_PROVIDER</code> 改成 <code>xtts</code>，重启服务；
        <br />② <b>云端效果更好</b>：填 <code>VC_CLOUD_BASE_URL</code> /
        <code>VC_CLOUD_API_KEY</code>，并把 <code>VC_PROVIDER</code> 改成 <code>cloud</code>。
      </NAlert>
      <NAlert v-else-if="status && !available" type="warning" :bordered="false" size="small">
        {{ status.note }}
      </NAlert>

      <template v-if="available">
        <div class="rec-tip">
          <b>怎么录：</b>安静环境里对着麦克风<b>连续朗读 15~30 秒</b>（随便念一段新闻或
          手边任何文字都行），别停顿太久、别念太快。系统会自动去噪、裁掉首尾空白，
          这段录音就是音色的来源 —— 之后就能用它念任何稿子。
        </div>

        <div class="upload-row">
          <NInput
            v-model:value="nameInput"
            size="small"
            placeholder="音色名称（可留空自动生成）"
            style="flex: 1; min-width: 140px"
            :disabled="!!running"
          />
          <NUpload
            :file-list="files"
            accept=".wav,.mp3,.m4a,.aac,.flac,.ogg,.opus,.webm,.amr"
            :max="1"
            :show-file-list="false"
            :custom-request="() => {}"
            :disabled="!!running"
            @change="onUpload"
          >
            <NButton
              size="small"
              type="primary"
              :loading="uploading"
              :disabled="!!running || cloneGate.blocked.value"
            >
              {{
                uploading
                  ? '上传中…'
                  : cloneGate.blocked.value
                    ? '无法复刻'
                    : cloneGate.left.value === null
                      ? '上传录音，开始复刻'
                      : `上传录音，开始复刻（剩 ${cloneGate.left.value}）`
              }}
            </NButton>
          </NUpload>
        </div>
        <div v-if="cloneGate.reason" class="hint gate-tip">⚠ {{ cloneGate.reason }}</div>

        <div v-if="running" class="rec-progress">
          <div class="rec-stage">
            {{ running.stage }} · {{ running.name }}
            <span class="hint">（首次复刻要加载模型，可能要一两分钟）</span>
          </div>
          <NProgress
            type="line"
            size="small"
            :percentage="running.progress"
            :show-indicator="false"
          />
        </div>

        <div v-if="voices.length" class="voice-list">
          <div v-for="v in voices" :key="v.id" class="voice-row">
            <div class="voice-main">
              <div class="voice-name">
                {{ v.name }}
                <NTag
                  v-if="v.quality?.level === 'warn'"
                  size="tiny"
                  type="warning"
                  :bordered="false"
                >
                  录音偏短
                </NTag>
              </div>
              <div class="voice-meta">
                {{ v.provider }} · 样本 {{ v.duration }}s
                <template v-if="v.quality?.message"> · {{ v.quality.message }}</template>
              </div>
            </div>
            <audio class="voice-audio" :src="audioUrl(v.sample_url)" controls preload="none" />
            <NButton size="tiny" quaternary @click="onRename(v)">改名</NButton>
            <NPopconfirm @positive-click="onDelete(v)">
              <template #trigger>
                <NButton size="tiny" quaternary type="error">删除</NButton>
              </template>
              删除这个音色？（已渲染的成片不受影响）
            </NPopconfirm>
          </div>
        </div>
      </template>
    </div>
  </div>
</template>

<style scoped>
.clone-box {
  border: 1px solid var(--app-border);
  border-radius: 10px;
  margin: 2px 0 16px;
  overflow: hidden;
}
.clone-head {
  background: var(--app-primary-a06);
}
.clone-toggle {
  width: 100%;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  background: transparent;
  border: 0;
  color: var(--app-text);
  font-size: 13px;
  cursor: pointer;
  text-align: left;
}
.clone-toggle:hover {
  color: var(--app-primary);
}
.arrow {
  display: inline-block;
  transition: transform 0.15s;
  color: var(--app-text-3);
  font-size: 11px;
}
.arrow.on {
  transform: rotate(90deg);
}
.clone-body {
  padding: 10px 12px 12px;
  border-top: 1px solid var(--app-border);
}
.rec-tip {
  font-size: 12px;
  color: var(--app-text-2);
  line-height: 1.85;
  margin: 8px 0 10px;
}
.rec-tip b {
  color: var(--app-text);
}
.upload-row {
  display: flex;
  gap: 8px;
  align-items: center;
  flex-wrap: wrap;
}
.rec-progress {
  margin-top: 10px;
}
.rec-stage {
  font-size: 12px;
  color: var(--app-text-2);
  margin-bottom: 4px;
}
.voice-list {
  margin-top: 10px;
  border-top: 1px solid var(--app-surface-3);
}
.voice-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 0;
  border-bottom: 1px solid var(--app-surface-3);
  flex-wrap: wrap;
}
.voice-row:last-child {
  border-bottom: 0;
}
.voice-main {
  flex: 1;
  min-width: 120px;
}
.voice-name {
  font-size: 13px;
  color: var(--app-text);
  display: flex;
  align-items: center;
  gap: 6px;
}
.voice-meta {
  font-size: 11.5px;
  color: var(--app-text-3);
  margin-top: 2px;
}
.voice-audio {
  height: 28px;
  max-width: 190px;
}
.hint {
  font-size: 11.5px;
  color: var(--app-text-3);
}

/* 权限/配额挡住时的常驻说明 */
.gate-tip {
  color: #d03050;
  margin-top: 6px;
}
</style>
