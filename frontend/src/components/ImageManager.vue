<script setup lang="ts">
/**
 * 配图管理：封面（模板生成）+ AI 文内配图 + 手动上传 / 列表管理。
 *
 * 两条生成通道是分开的：
 *  - 封面：HTML 模板 + 无头浏览器截图，文字确定、品牌统一，固定在配图首位；
 *  - 配图：AI 文生图（IMAGE_SCRIPT），随机、按提示词，插在正文里。
 * 未配置对应脚本时按钮禁用并提示手动上传。
 */
import { computed, ref } from 'vue'
import {
  NDivider,
  NEmpty,
  NInput,
  NInputNumber,
  NRadioButton,
  NRadioGroup,
  NSpin,
  NTag,
  NUpload,
  useMessage,
} from 'naive-ui'
import IconTextBtn from './IconTextBtn.vue'
import type { UploadFileInfo } from 'naive-ui'
import {
  deleteImage,
  generateCover,
  generateImages,
  updateImage,
  uploadImage,
} from '@/api/layout'
import { usePermGate } from '@/composables/usePermGate'
import type { ContentImage } from '@/types'

/**
 * 生图与封面的门控：**只受权限管控，不计次**。
 *
 * 刻意不给它们配配额：这两个动作走的是本机脚本/无头浏览器，不烧 LLM token、
 * 不占渲染队列，成本与「对话/视频/排版」不在一个量级。给它们计次只会让
 * 用户在面对一个免费功能时也被拦，权限不够另说。
 */
const imageGenGate = usePermGate({ perm: 'layout.image_gen' })
const coverGate = usePermGate({ perm: 'layout.cover' })

const props = defineProps<{
  contentId: string
  images: ContentImage[]
  /** 生图脚本是否可用（不可用则禁用生成按钮） */
  genReady: boolean
  /** 封面通道是否可用 */
  coverReady?: boolean
  /** 可选封面风格 { id: 说明 } */
  coverStyles?: Record<string, string>
  /** 封面默认风格 */
  coverDefaultStyle?: string
  /** 封面默认标签 */
  coverDefaultTag?: string
  /** 正文（用于预填封面标题/副标题） */
  body?: string
  /**
   * 只读：已发布任务的「排版详情」不该还能改配图。
   * 只保留预览，隐藏全部生成/上传/编辑/删除入口。
   */
  readonly?: boolean
}>()

const isReadonly = computed(() => !!props.readonly)

const emit = defineEmits<{ (e: 'changed', images: ContentImage[]): void }>()

const message = useMessage()
const prompt = ref('')
const count = ref(1)
const generating = ref(false)
const uploading = ref(false)

// ---- 封面表单 ----
const coverTitle = ref('')
const coverSub = ref('')
const coverTag = ref('')
const coverStyle = ref('gradient')
const coverGen = ref(false)
const showCoverForm = ref(false)

const styleOptions = computed(() => {
  const s = props.coverStyles || {}
  return Object.keys(s).length
    ? Object.entries(s).map(([value, label]) => ({ value, label }))
    : [
        { value: 'gradient', label: '渐变' },
        { value: 'dark', label: '深色' },
        { value: 'light', label: '浅色' },
      ]
})

const imgs = computed(() => props.images)
const coverImg = computed(() => imgs.value.find((i) => i.source === 'cover'))
const bodyImgs = computed(() => imgs.value.filter((i) => i.source !== 'cover'))

/** 从正文粗略解析标题/副标题，用于预填封面表单（用户可改）。 */
function deriveFromBody(): { title: string; sub: string } {
  const raw = (props.body || '').split('\n')
  const clean = (s: string) =>
    s
      .replace(/^\s*#{1,6}\s*/, '')
      .replace(/^\s*[>\-*+]\s*/, '')
      .replace(/\*/g, '')
      .replace(/`/g, '')
      .trim()
  const lines = raw.map(clean).filter(Boolean)
  let title = lines[0] || ''
  const sub = (lines[1] || '').slice(0, 20)
  if (title.length > 18) {
    const mid = Math.floor(title.length / 2)
    for (let off = 0; off < mid; off++) {
      let done = false
      for (const idx of [mid - off, mid + off]) {
        if (idx > 0 && idx < title.length && '、，。！？；： ,.!?;:'.includes(title[idx])) {
          title = title.slice(0, idx) + '\n' + title.slice(idx + 1)
          done = true
          break
        }
      }
      if (done) break
    }
  }
  return { title, sub }
}

function openCoverForm() {
  if (!coverTitle.value) {
    const { title, sub } = deriveFromBody()
    coverTitle.value = title
    coverSub.value = coverSub.value || sub
  }
  if (!coverTag.value) coverTag.value = props.coverDefaultTag || ''
  coverStyle.value = props.coverDefaultStyle || coverStyle.value || 'gradient'
  showCoverForm.value = true
}

async function onGenerateCover() {
  if (!coverTitle.value.trim()) {
    message.warning('先填封面标题')
    return
  }
  coverGen.value = true
  try {
    const res = await generateCover(props.contentId, {
      title: coverTitle.value.trim(),
      sub: coverSub.value.trim(),
      tag: coverTag.value.trim(),
      style: coverStyle.value,
    })
    emit('changed', res.images)
    message.success(coverImg.value ? '封面已替换' : '封面已生成')
  } catch (e) {
    message.error(`生成封面失败：${(e as Error).message}`)
  } finally {
    coverGen.value = false
  }
}

/** 把已上传的普通图设为封面（移到首位并打标）——复用「改 position」能力。 */
async function makeCover(img: ContentImage) {
  try {
    const res = await updateImage(props.contentId, img.id, { position: 0 })
    emit('changed', res.images)
    message.success('已设为封面位置')
  } catch {
    message.error('设置失败')
  }
}

async function onGenerate() {
  if (!prompt.value.trim()) {
    message.warning('先写一句配图描述')
    return
  }
  generating.value = true
  try {
    const res = await generateImages(props.contentId, {
      prompt: prompt.value.trim(),
      count: count.value,
    })
    emit('changed', res.images)
    message.success(`已生成 ${count.value} 张配图`)
    prompt.value = ''
  } catch (e) {
    message.error(`生图失败：${(e as Error).message}`)
  } finally {
    generating.value = false
  }
}

/** 手动上传（naive 的 NUpload 走自定义请求）。 */
async function onUpload({ file }: { file: UploadFileInfo }) {
  if (!file.file) return
  uploading.value = true
  try {
    const res = await uploadImage(props.contentId, file.file)
    emit('changed', res.images)
    message.success('已上传')
  } catch (e) {
    message.error(`上传失败：${(e as Error).message}`)
  } finally {
    uploading.value = false
  }
  return false // 阻止默认上传行为
}

async function onDelete(img: ContentImage) {
  try {
    const res = await deleteImage(props.contentId, img.id)
    emit('changed', res.images)
    message.success('已删除')
  } catch (e) {
    message.error(`删除失败：${(e as Error).message}`)
  }
}

async function saveAlt(img: ContentImage, alt: string) {
  if (img.alt === alt) return
  try {
    const res = await updateImage(props.contentId, img.id, { alt })
    emit('changed', res.images)
  } catch {
    message.error('保存说明失败')
  }
}
</script>

<template>
  <div class="img-mgr">
    <!-- ============ 封面 ============ -->
    <div class="cover-block">
      <div class="cover-head">
        <span class="block-title">封面（头图）</span>
        <NTag v-if="coverImg" size="tiny" type="success" :bordered="false">已生成</NTag>
        <span class="cover-tip">模板化生成，文字确定、风格统一；固定在正文最前</span>
      </div>

      <!-- 已有封面：预览 + 操作 -->
      <div v-if="coverImg" class="cover-preview">
        <img :src="coverImg.url" :alt="coverImg.alt || '封面'" />
        <div v-if="!isReadonly" class="cover-ops">
          <IconTextBtn name="image" size="tiny" :disabled="!coverReady" :loading="coverGen" @click="openCoverForm">
            换一张
          </IconTextBtn>
          <IconTextBtn name="trash" size="tiny" quaternary type="error" @click="onDelete(coverImg)">
            删除
          </IconTextBtn>
        </div>
      </div>

      <!-- 无封面：直接展开表单（只读时不显示生成入口） -->
      <div v-else-if="coverReady && !isReadonly" class="cover-empty">
        <IconTextBtn
          name="image"
          type="primary"
          ghost
          :disabled="coverGate.blocked.value"
          @click="openCoverForm"
        >
          {{ coverGate.blocked.value ? '无权限' : '生成封面' }}
        </IconTextBtn>
        <span class="hint-inline">或上传图片后点「设为封面」</span>
      </div>
      <div v-else-if="!coverReady && !isReadonly" class="hint warn">
        封面通道不可用：请确认 <code>COVER_SCRIPT</code> 指向的脚本存在，且本机装有
        Chrome/Chromium。也可「上传图片」后点「设为封面」。
      </div>
      <div v-else class="hint-inline">无封面</div>

      <!-- 生成/换封面表单（只读时不渲染） -->
      <div v-if="showCoverForm && coverReady && !isReadonly" class="cover-form">
        <NInput
          v-model:value="coverTitle"
          size="small"
          placeholder="封面主标题（用 \n 换行，建议 6~16 字）"
          :disabled="coverGen"
          @keydown.enter.prevent
        />
        <NInput
          v-model:value="coverSub"
          size="small"
          placeholder="副标题（可省，建议 ≤20 字）"
          :disabled="coverGen"
        />
        <NInput
          v-model:value="coverTag"
          size="small"
          placeholder="左上角标签（可省，如：效率指南 · 第 3 期）"
          :disabled="coverGen"
        />
        <div class="cover-form-actions">
          <NRadioGroup v-model:value="coverStyle" size="small" :disabled="coverGen">
            <NRadioButton v-for="s in styleOptions" :key="s.value" :value="s.value">
              {{ s.label }}
            </NRadioButton>
          </NRadioGroup>
          <div class="cover-btns">
            <IconTextBtn
              name="image"
              type="primary"
              :loading="coverGen"
              :disabled="coverGate.blocked.value"
              @click="onGenerateCover"
            >
              {{ coverGate.blocked.value ? '无权限' : coverImg ? '重新生成' : '生成' }}
            </IconTextBtn>
            <IconTextBtn name="close" size="small" quaternary :disabled="coverGen" @click="showCoverForm = false">
              收起
            </IconTextBtn>
          </div>
          <div v-if="coverGate.reason" class="hint warn gate-tip">
            {{ coverGate.reason }}
          </div>
        </div>
      </div>
    </div>

    <NDivider style="margin: 10px 0" />

    <!-- ============ 文内配图 ============ -->
    <div class="block-title">
      文内配图（AI 生成 / 上传）
      <span v-if="!isReadonly" class="sub-tip">AI 生成需配置脚本；「上传图片」从本机选图</span>
    </div>
    <div v-if="!isReadonly" class="gen-row">
      <NInput
        v-model:value="prompt"
        class="prompt-input"
        size="small"
        placeholder="配图描述，如：一杯咖啡旁的笔记本电脑，温暖光线"
        :disabled="generating"
      />
      <div class="gen-actions">
        <span class="count-label">张数</span>
        <NInputNumber
          v-model:value="count"
          size="small"
          :min="1"
          :max="4"
          style="width: 92px"
          :disabled="generating"
        />
        <IconTextBtn
          name="image"
          type="primary"
          :loading="generating"
          :disabled="!genReady || imageGenGate.blocked.value"
          @click="onGenerate"
        >
          {{ imageGenGate.blocked.value ? '无权限' : '生成配图' }}
        </IconTextBtn>
        <div v-if="imageGenGate.reason" class="hint warn gate-tip">
          {{ imageGenGate.reason }}
        </div>
        <NUpload
          :show-file-list="false"
          :custom-request="onUpload as never"
          accept="image/*"
          :disabled="uploading"
        >
          <IconTextBtn name="upload" size="small" :loading="uploading" :disabled="uploading">上传图片</IconTextBtn>
        </NUpload>
      </div>
    </div>
    <div v-if="!genReady && !isReadonly" class="hint warn">
      <b>「生成配图」暂不可用</b>：后端未配置生图脚本（<code>IMAGE_SCRIPT</code> 为空）。
      <br />可用下面两种方式补图：① 点「上传图片」从本机选图（存进本内容的配图目录，随正文排版一起用）；
      ② 在 <code>backend/.env</code> 里把 <code>IMAGE_SCRIPT</code> 指向一个支持
      <code>--prompt/--size/--count/--out</code> 的脚本后重启后端。
    </div>

    <NSpin :show="generating">
      <NEmpty
        v-if="!bodyImgs.length"
        size="small"
        description="还没有文内配图"
        style="margin: 14px 0"
      />
      <div v-else class="grid">
        <div v-for="img in bodyImgs" :key="img.id" class="cell">
          <div class="thumb">
            <img :src="img.url" :alt="img.alt" />
          </div>
          <div class="cell-actions">
            <input
              v-if="!isReadonly"
              class="alt-input"
              :value="img.alt"
              placeholder="图片说明（可留空）"
              @change="saveAlt(img, ($event.target as HTMLInputElement).value)"
            />
            <span v-else class="alt-readonly">{{ img.alt || '（无说明）' }}</span>
            <template v-if="!isReadonly">
              <IconTextBtn
                class="del-btn"
                name="pin"
                size="tiny"
                quaternary
                @click="makeCover(img)"
              >
                设为封面
              </IconTextBtn>
              <IconTextBtn
                class="del-btn"
                name="trash"
                size="tiny"
                quaternary
                type="error"
                @click="onDelete(img)"
              >
                删除
              </IconTextBtn>
            </template>
          </div>
        </div>
      </div>
    </NSpin>
  </div>
</template>

<style scoped>
.block-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--app-text-2);
}
.sub-tip {
  font-weight: 400;
  font-size: 12px;
  color: var(--app-text-3);
  margin-left: 6px;
}
/* ---------- 封面 ---------- */
.cover-block {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.cover-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.cover-tip {
  font-size: 12px;
  color: var(--app-text-3);
}
.cover-preview {
  display: flex;
  gap: 12px;
  align-items: flex-start;
}
.cover-preview img {
  width: min(420px, 100%);
  border-radius: 8px;
  border: 1px solid var(--app-border, #e0e0e6);
  display: block;
}
.cover-ops {
  display: flex;
  flex-direction: column;
  gap: 6px;
  flex-shrink: 0;
}
.cover-empty {
  display: flex;
  align-items: center;
  gap: 10px;
}
.hint-inline {
  font-size: 12px;
  color: var(--app-text-3);
}
.cover-form {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 10px;
  border: 1px dashed var(--app-border, #e0e0e6);
  border-radius: 8px;
  background: rgba(128, 128, 128, 0.04);
}
.cover-form-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  flex-wrap: wrap;
}
.cover-btns {
  display: flex;
  gap: 8px;
  flex-shrink: 0;
}

.gen-row {
  display: flex;
  gap: 8px;
  align-items: center;
  margin-top: 10px;
  /* 描述再长也不换行挤走按钮：输入框可收缩，按钮组不收缩 */
}
/* 关键：min-width:0 让 flex 子项能收缩，否则长 placeholder/value 会撑破整行 */
.gen-row .prompt-input {
  flex: 1 1 220px;
  min-width: 0;
}
.gen-actions {
  display: flex;
  gap: 8px;
  align-items: center;
  flex-shrink: 0;
}
.count-label {
  font-size: 12px;
  color: var(--app-text-3);
  flex-shrink: 0;
}
@media (max-width: 768px) {
  .gen-row {
    flex-wrap: wrap;
  }
  .gen-row .prompt-input {
    flex-basis: 100%;
  }
  .cover-preview {
    flex-direction: column;
  }
  .cover-ops {
    flex-direction: row;
  }
}
.hint.warn {
  margin-top: 10px;
  font-size: 12px;
  line-height: 1.6;
  color: var(--app-amber-text);
  background: rgba(250, 200, 100, 0.12);
  padding: 8px 12px;
  border-radius: 6px;
}

/* 权限挡住时的常驻说明：比 warn 更扎眼，用红色 */
.gate-tip {
  color: #d03050;
  background: rgba(208, 48, 80, 0.1);
}
.grid {
  display: grid;
  /* 宽面板（全屏后）不会过密、窄面板也不会挤：留 160px 下限 + 320px 上限 */
  grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
  gap: 12px;
  margin-top: 14px;
}
.cell {
  border: 1px solid var(--app-border, #e0e0e6);
  border-radius: 8px;
  overflow: hidden;
  background: rgba(128, 128, 128, 0.04);
  display: flex;
  flex-direction: column;
}
.thumb {
  width: 100%;
  aspect-ratio: 4 / 3;
  max-height: 190px;
  overflow: hidden;
  background: rgba(128, 128, 128, 0.08);
}
.thumb img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
}
.cell-actions {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 8px;
  border-top: 1px solid var(--app-border, #e0e0e6);
}
.alt-input {
  flex: 1;
  min-width: 0;
  border: none;
  background: transparent;
  font-size: 12px;
  color: inherit;
  outline: none;
  padding: 2px 0;
}
.alt-input::placeholder {
  color: var(--app-text-3);
}
/* 只读态下的图片说明：与可编辑输入同样的排版，只是不可改 */
.alt-readonly {
  flex: 1;
  min-width: 0;
  font-size: 12px;
  color: var(--app-text-2);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.del-btn {
  flex-shrink: 0;
}
</style>
