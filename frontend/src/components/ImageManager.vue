<script setup lang="ts">
/**
 * 配图管理：封面（模板生成）+ AI 文内配图 + 手动上传 / 列表管理。
 *
 * 两条生成通道是分开的：
 *  - 封面：HTML 模板 + 无头浏览器截图，文字确定、品牌统一，固定在配图首位；
 *  - 配图：AI 文生图（IMAGE_SCRIPT），随机、按提示词，插在正文里。
 * 未配置对应脚本时按钮禁用并提示手动上传。
 */
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import {
  NButton,
  NDivider,
  NEmpty,
  NImage,
  NInput,
  NInputNumber,
  NRadioButton,
  NRadioGroup,
  NSpin,
  NTag,
  NTooltip,
  NUpload,
  useMessage,
} from 'naive-ui'
import IconTextBtn from './IconTextBtn.vue'
import AssetPicker from './AssetPicker.vue'
import type { UploadFileInfo } from 'naive-ui'
import {
  deleteImage,
  generateCover,
  generateImages,
  getImageProviders,
  updateImage,
  uploadImage,
} from '@/api/layout'
import { attachAssetToContent, type Asset } from '@/api/assets'
import { usePermGate } from '@/composables/usePermGate'
import { useRouter } from 'vue-router'
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
const crawlGate = usePermGate({ perm: 'assets.crawl' })
const router = useRouter()

/**
 * 从「文章排版后配图」直达采集弹层。
 * 采集是素材库页的能力，但用户的使用场景在文章里 —— 带 content_id 跳过去，
 * 素材库页会自动打开弹层并预选这篇文章，不用回去翻列表找刚写的那篇。
 */
function goCrawlForArticle() {
  void router.push({ name: 'assets', query: { crawl: '1', content_id: props.contentId } })
}

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
  /** 当前排版主题 id：封面配色跟随该主题（留空用默认品牌色） */
  theme?: string
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

// ---- 素材库选取 ----
const assetPickerShow = ref(false)

/**
 * 从素材库选了一张：由后端把素材插进 content.images，再用返回的完整列表覆盖本地。
 *
 * 为什么不让弹窗自己插：弹窗的职责是「选」，落地方式该由使用方决定
 * （配图面板要插进文章，别处可能只是取个链接）。职责分开后弹窗可复用。
 */
async function onAssetPicked(a: Asset) {
  try {
    const r = await attachAssetToContent(props.contentId, a.id)
    emit('changed', r.images as ContentImage[])
    message.success(`已插入「${a.title || '素材'}」`)
  } catch {
    message.error('插入素材失败')
  }
}

// ---- 封面表单 ----
const coverTitle = ref('')
const coverSub = ref('')
const coverTag = ref('')
const coverStyle = ref('gradient')
const coverGen = ref(false)
const showCoverForm = ref(false)

// ---- 文内配图：生成通道（脚本 / Cloudflare AI）----
const genProvider = ref<'script' | 'cloudflare'>('script')
const providers = ref<{ script: boolean; cloudflare: boolean }>({ script: false, cloudflare: false })
onMounted(async () => {
  try {
    providers.value = await getImageProviders()
    // 默认选第一个可用的通道
    if (!providers.value.script && providers.value.cloudflare) genProvider.value = 'cloudflare'
  } catch {
    /* 能力位拿不到就按脚本兜底 */
  }
})

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
      theme: props.theme,
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
      provider: genProvider.value,
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

/**
 * 图片说明（alt）编辑：底部操作行只放一个铅笔图标，点击后在缩略图与操作行
 * 之间展开全宽输入框。原因是格子最窄 160px，常驻输入框会被「设为封面/删除」
 * 挤得只剩 placeholder 头两个字；改成图标后底部行还余量，后续加「文内视频」
 * 之类的操作也只是换行不挤压。
 */
const editingAltId = ref<string | null>(null)
const altDraft = ref('')
const altEditEl = ref<HTMLInputElement | null>(null)

function startEditAlt(img: ContentImage) {
  altDraft.value = img.alt || ''
  editingAltId.value = img.id
}

// 函数 ref：输入框在 v-for 里，字符串 ref 会被 Vue 收成数组（拿到的不再是 DOM 元素）
function setAltEl(el: unknown) {
  altEditEl.value = (el as HTMLInputElement | null) ?? null
}

// 编辑行出现后主动聚焦：autofocus 属性会被 NModal 的焦点陷阱压掉，显式 focus() 才稳
watch(editingAltId, async (id) => {
  if (!id) return
  await nextTick()
  altEditEl.value?.focus()
})

async function confirmAlt(img: ContentImage) {
  // 防重入：回车与失焦可能连着触发，只认第一发
  if (editingAltId.value !== img.id) return
  editingAltId.value = null
  const alt = altDraft.value.trim()
  if (alt === (img.alt || '')) return
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
        <NImage
          class="cover-img"
          :src="coverImg.url"
          :alt="coverImg.alt || '封面'"
          object-fit="cover"
          lazy
        />
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
          <div v-if="coverGate.reason.value" class="hint warn gate-tip">
            {{ coverGate.reason.value }}
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
        type="textarea"
        :autosize="{ minRows: 2, maxRows: 5 }"
        placeholder="配图描述，如：一杯咖啡旁的笔记本电脑，温暖光线，桌面平铺俯拍"
        :disabled="generating"
      />
    </div>
    <div v-if="!isReadonly" class="gen-actions">
      <NRadioGroup v-model:value="genProvider" size="small" :disabled="generating">
        <NRadioButton v-if="providers.script" value="script">脚本生成</NRadioButton>
        <NRadioButton v-if="providers.cloudflare" value="cloudflare">Cloudflare AI</NRadioButton>
      </NRadioGroup>
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
      <NUpload
        :show-file-list="false"
        :custom-request="onUpload as never"
        accept="image/*"
        :disabled="uploading"
      >
        <IconTextBtn name="upload" size="small" :loading="uploading" :disabled="uploading">上传图片</IconTextBtn>
      </NUpload>
      <!-- 素材库：与「上传图片」并列 —— 一个是现在找图，一个是用攒下来的图 -->
      <NButton
        v-if="!isReadonly"
        size="small"
        secondary
        style="margin-left: 6px"
        @click="assetPickerShow = true"
      >
        从素材库选
      </NButton>
      <AssetPicker v-model:show="assetPickerShow" @picked="onAssetPicked" />
      <!-- 与「从素材库选」并列：一个用攒下来的图，一个按这篇文章现找 -->
      <NButton
        v-if="!isReadonly"
        size="small"
        secondary
        :disabled="crawlGate.blocked.value"
        :title="crawlGate.blocked.value ? crawlGate.reason.value : '按当前文章内容找配图'"
        @click="goCrawlForArticle"
      >
        按文章找配图
      </NButton>
      <div v-if="imageGenGate.reason.value" class="hint warn gate-tip">
        {{ imageGenGate.reason.value }}
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
            <NImage
              class="thumb-img"
              :src="img.url"
              :alt="img.alt || '配图'"
              object-fit="cover"
              lazy
            />
          </div>
          <!-- 说明编辑行：点铅笔图标后展开在缩略图与操作行之间，全宽不挤压 -->
          <div v-if="!isReadonly && editingAltId === img.id" class="cell-alt-edit">
            <input
              :ref="setAltEl"
              v-model="altDraft"
              class="alt-edit-input"
              placeholder="图片说明（可留空）"
              @keydown.enter.prevent="confirmAlt(img)"
              @keydown.esc.prevent="editingAltId = null"
              @blur="confirmAlt(img)"
            />
          </div>
          <div class="cell-actions">
            <!-- 可编辑态：铅笔图标（已填说明带小圆点）；只读态：直接显示说明文字 -->
            <NTooltip v-if="!isReadonly" trigger="hover">
              <template #trigger>
                <IconTextBtn
                  class="alt-btn"
                  :class="{ 'has-alt': !!img.alt }"
                  name="edit"
                  size="tiny"
                  quaternary
                  @mousedown.prevent
                  @click="startEditAlt(img)"
                />
              </template>
              {{ img.alt ? `图片说明：${img.alt}` : '添加图片说明' }}
            </NTooltip>
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
.cover-img {
  width: min(420px, 100%);
  border-radius: 8px;
  display: block;
}
.cover-img :deep(img) {
  width: 100%;
  border-radius: 8px;
  border: 1px solid var(--app-border, #e0e0e6);
  display: block;
  cursor: zoom-in;
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

/* 描述框独占一行（多行文本域 2~5 行自适应高度），按钮组在下一行：
   再窄也是换行而非挤压，「张数」永远看得见 */
.gen-row {
  margin-top: 10px;
}
.gen-row .prompt-input {
  width: 100%;
}
.gen-actions {
  display: flex;
  gap: 8px;
  align-items: center;
  flex-wrap: wrap;
  margin-top: 8px;
}
.count-label {
  font-size: 12px;
  color: var(--app-text-3);
  flex-shrink: 0;
}
@media (max-width: 768px) {
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
.thumb-img {
  width: 100%;
  height: 100%;
}
.thumb-img :deep(img) {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
  cursor: zoom-in;
}
.cell-actions {
  display: flex;
  align-items: center;
  gap: 6px;
  /* 后续加「文内视频」等按钮时换行，不挤压现有按钮 */
  flex-wrap: wrap;
  padding: 6px 8px;
  border-top: 1px solid var(--app-border, #e0e0e6);
}
/* 铅笔图标按钮：不收缩；已填说明时右上角小圆点提示 */
.alt-btn {
  flex-shrink: 0;
  position: relative;
}
.alt-btn.has-alt::after {
  content: '';
  position: absolute;
  top: 3px;
  right: 3px;
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: var(--app-primary, #4f7cff);
}
/* 说明编辑行：临时展开，全宽输入 */
.cell-alt-edit {
  padding: 6px 8px 0;
}
.alt-edit-input {
  width: 100%;
  box-sizing: border-box;
  border: 1px solid var(--app-primary, #4f7cff);
  border-radius: 4px;
  background: var(--app-sunken, rgba(128, 128, 128, 0.08));
  color: inherit;
  font-size: 12px;
  padding: 4px 6px;
  outline: none;
}
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
