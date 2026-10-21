<script setup lang="ts">
/**
 * 个人中心：**和「我」相关的设置**都收在这里 —— 账号资料（昵称/邮箱）、
 * 我的使用配额、作者信息。
 *
 * 为什么从设置页拆出来：设置页（系统设置）讲的是「这套系统怎么样」——
 * 服务健康、AI 对话通道、工具清单、公众号凭证、环境变量；而昵称、配额、
 * 作者信息讲的是「我是谁、我还能用多少」。两类信息混在一页，找起来全靠
 * 缘分。入口在顶栏头像下拉（AppShell），不在侧栏菜单 —— 它是
 * 按用户走的，不是按功能模块走的。
 *
 * AI 对话供应商 2026-10-21 搬去了系统设置：那是系统级通道决策（影响
 * 所有账号的对话行为与计费口径），不是个人偏好。
 */
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import {
  NButton,
  NCard,
  NDescriptions,
  NDescriptionsItem,
  NInput,
  NSpin,
  NTag,
  useMessage,
} from 'naive-ui'

import { deleteAvatar, updateProfile, uploadAvatar } from '@/api/auth'
import { useAuthStore } from '@/stores/auth'
import IconTextBtn from '@/components/IconTextBtn.vue'
import { getAuthorInfo, updateAuthorInfo } from '@/api/settings'

const auth = useAuthStore()
const router = useRouter()
const message = useMessage()

// ---------------------------------------------------------------- 账号资料
const nickname = ref('')
const email = ref('')
const saving = ref(false)

const createdAtText = computed(() => {
  const ts = auth.user?.created_at
  if (!ts) return '-'
  return new Date(ts * 1000).toLocaleString('zh-CN', { hour12: false })
})

/** 没传头像时，圆徽里显示的字母（昵称优先，回落用户名）。 */
const avatarLetter = computed(() =>
  ((auth.user?.nickname || auth.user?.username || '?')[0] ?? '?').toUpperCase(),
)

// 头像上传：走隐藏的 file input（naive-ui 的 NUpload 那套拖拽区对
// 「就换一张图」来说太重），前端先挡一道大小，剩下的交给后端校验。
const avatarInputRef = ref<HTMLInputElement | null>(null)
const avatarUploading = ref(false)

function avatarPick() {
  avatarInputRef.value?.click()
}

async function onAvatarPick(e: Event) {
  const input = e.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = '' // 允许重复选同一个文件
  if (!file) return
  if (file.size > 2 * 1024 * 1024) {
    message.warning('图片超过 2MB，请压缩后再传')
    return
  }
  avatarUploading.value = true
  try {
    await uploadAvatar(file)
    message.success('头像已更新')
    // 顶栏头像来自 auth.user，刷新一次立刻生效（不然要等 60 秒静默刷新）
    await auth.refreshMe()
  } catch (err) {
    message.error(`上传失败：${(err as Error).message}`)
  } finally {
    avatarUploading.value = false
  }
}

async function onAvatarDelete() {
  avatarUploading.value = true
  try {
    await deleteAvatar()
    message.success('头像已删除')
    await auth.refreshMe()
  } catch (err) {
    message.error(`删除失败：${(err as Error).message}`)
  } finally {
    avatarUploading.value = false
  }
}

async function saveProfile() {
  saving.value = true
  try {
    // 空串 = 清空（后端把空串归一成 NULL，显示名回落 username）
    await updateProfile({ nickname: nickname.value.trim(), email: email.value.trim() })
    message.success('已保存')
    // 顶栏昵称来自 auth.user，刷新一次立刻生效（不然要等 60 秒静默刷新）
    await auth.refreshMe()
  } catch (e) {
    message.error(`保存失败：${(e as Error).message}`)
  } finally {
    saving.value = false
  }
}

// ---------------------------------------------------------------- 我的配额
interface QuotaRow {
  feature: string
  label: string
  used: number
  limit: number | null
  unlimited: boolean
  left: number | null
  periodLabel: string
  resetText: string
}

/**
 * 我的配额。只列**设了上限**的功能 —— 五个全列出来大半是「不限」，
 * 反而把真正有用的信息淹了。
 */
const quotaRows = computed<QuotaRow[]>(() => {
  const out: QuotaRow[] = []
  for (const [feature, snap] of Object.entries(auth.usage)) {
    if (!snap || snap.unlimited) continue
    out.push({
      feature,
      label: snap.label,
      used: snap.used,
      limit: snap.limit,
      unlimited: false,
      left: snap.left,
      periodLabel: snap.period === 'month' ? '每月' : '每日',
      resetText: fmtReset(snap.reset_at),
    })
  }
  return out
})

function fmtReset(ts: number): string {
  const d = new Date(ts * 1000)
  const p = (n: number) => String(n).padStart(2, '0')
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

// ---------------------------------------------------------------- 作者信息
const authorName = ref('')
const authorIntro = ref('')
const authorLoading = ref(false)
const authorSaving = ref(false)

// 占位符字面量（放常量里，避免模板里写 `{{...}}` 被 Vue 当插值解析）
const AUTHOR_PH_NAME = '{{作者名}}'
const AUTHOR_PH_INTRO = '{{一句话简介}}'

async function loadAuthor() {
  authorLoading.value = true
  try {
    const a = await getAuthorInfo()
    authorName.value = a.author_name || ''
    authorIntro.value = a.author_intro || ''
  } catch (e) {
    console.warn('加载作者信息失败', (e as Error).message)
  } finally {
    authorLoading.value = false
  }
}

async function saveAuthor() {
  authorSaving.value = true
  try {
    await updateAuthorInfo({
      author_name: authorName.value.trim(),
      author_intro: authorIntro.value.trim(),
    })
    message.success('已保存')
  } catch (e) {
    message.error(`保存失败：${(e as Error).message}`)
  } finally {
    authorSaving.value = false
  }
}

// ---------------------------------------------------------------- AI 对话供应商
// （2026-10-21 搬到系统设置页：这是系统级通道决策——影响所有账号的对话
// 行为与计费口径，不是「我是谁」式的个人偏好。切换权与只读逻辑一并带走。）

onMounted(() => {
  nickname.value = auth.user?.nickname ?? ''
  email.value = auth.user?.email ?? ''
  void loadAuthor()
})
</script>

<template>
  <div class="profile-page">
    <!-- 账号：资料三件套 + 数据归属说明 -->
    <NCard size="small" title="账号资料" style="margin-bottom: 16px">
      <!-- 头像：大图预览 + 更换/删除。隐藏 file input 由「更换头像」点起来 -->
      <div class="avatar-row">
        <img
          v-if="auth.user?.avatar_url"
          class="avatar-preview"
          :src="auth.user.avatar_url"
          alt="头像"
        />
        <span v-else class="avatar-preview avatar-fallback">{{ avatarLetter }}</span>
        <div class="avatar-actions">
          <div class="avatar-btns">
            <IconTextBtn name="upload" type="primary" :loading="avatarUploading" @click="avatarPick">
              更换头像
            </IconTextBtn>
            <NButton
              v-if="auth.user?.avatar_url"
              size="tiny"
              quaternary
              :disabled="avatarUploading"
              @click="onAvatarDelete"
            >
              删除
            </NButton>
          </div>
          <span class="field-note">
            支持 png / jpg / webp，不超过 2MB；上传后自动裁剪为 256×256 正方形
          </span>
        </div>
        <input
          ref="avatarInputRef"
          type="file"
          accept="image/png,image/jpeg,image/webp"
          hidden
          @change="onAvatarPick"
        />
      </div>

      <NDescriptions :column="2" size="small" label-placement="left" bordered>
        <NDescriptionsItem label="用户名">
          <code>{{ auth.user?.username }}</code>
          <span class="field-note">（登录用，不可修改）</span>
        </NDescriptionsItem>
        <NDescriptionsItem label="角色">
          <NTag
            size="small"
            :bordered="false"
            :type="auth.isAdmin ? 'warning' : 'default'"
          >
            {{ auth.isAdmin ? '管理员' : '普通成员' }}
          </NTag>
        </NDescriptionsItem>
        <NDescriptionsItem label="注册时间">
          {{ createdAtText }}
        </NDescriptionsItem>
        <NDescriptionsItem label="邀请码">
          <code v-if="auth.user?.invite_code">{{ auth.user.invite_code }}</code>
          <span v-else class="field-note">-</span>
        </NDescriptionsItem>
      </NDescriptions>

      <div class="profile-form">
        <div class="form-row">
          <span class="form-label">昵称</span>
          <NInput
            v-model:value="nickname"
            size="small"
            style="width: 220px"
            placeholder="未设置时显示用户名"
            maxlength="64"
          />
          <span class="field-note">顶栏和个人中心显示它，留空则回落用户名</span>
        </div>
        <div class="form-row">
          <span class="form-label">邮箱</span>
          <NInput
            v-model:value="email"
            size="small"
            style="width: 220px"
            placeholder="选填，仅作联系方式"
            maxlength="128"
          />
          <span class="field-note">不用于登录；已被他人占用的邮箱保存不了</span>
        </div>
        <div class="form-row">
          <span class="form-label" />
          <IconTextBtn name="check" type="primary" :loading="saving" @click="saveProfile">
            保存资料
          </IconTextBtn>
        </div>
      </div>

      <div class="api-base-hint" style="margin-top: 4px; opacity: 0.75">
        内容、发布队列、公众号账号、AI
        用量、复刻音色都按登录身份隔离，互相不可见。
      </div>
    </NCard>

    <!-- 我的配额：按钮级权限管控的几个花钱功能，还剩多少一眼可见 -->
    <NCard v-if="quotaRows.length" size="small" title="我的使用配额" style="margin-bottom: 16px">
      <div v-for="q in quotaRows" :key="q.feature" class="quota-line">
        <span class="quota-name">{{ q.label }}</span>
        <span v-if="q.unlimited" class="quota-val muted">不限</span>
        <span v-else class="quota-val" :class="{ danger: q.left === 0 }">
          {{ q.used }} / {{ q.limit }}
          <span class="quota-period">（{{ q.periodLabel }}，{{ q.resetText }} 重置）</span>
        </span>
      </div>
      <div class="quota-note">
        超额会被直接拦下（HTTP 429）。次数按北京时间计算，提交即预扣 ——
        异步任务（渲染/排版/复刻）资源在提交时就已经花出去了，失败不退还；
        只有推送草稿能同步判成败，失败会自动退还。
      </div>
    </NCard>

    <!-- 作者信息：排版时自动替换正文里的 {{作者名}} / {{一句话简介}} 占位符 -->
    <NCard size="small" title="作者信息" style="margin-bottom: 16px">
      <NSpin :show="authorLoading">
        <div class="author-form">
          <NInput
            v-model:value="authorName"
            size="small"
            style="width: 180px"
            placeholder="作者名（如：甲木）"
          />
          <NInput
            v-model:value="authorIntro"
            size="small"
            style="width: 340px"
            placeholder="一句话简介（如：专注 AI 观察与干货）"
          />
          <IconTextBtn name="check" type="primary" :loading="authorSaving" @click="saveAuthor">
            保存
          </IconTextBtn>
        </div>
      </NSpin>
      <div class="api-base-hint" style="margin-top: 8px; opacity: 0.75">
        排版时，正文里的
        <code v-text="AUTHOR_PH_NAME" /> 和 <code v-text="AUTHOR_PH_INTRO" />
        会自动替换成这里填的值。留空则不替换（保留占位符提醒你去填）。
        加工排版面板里也能临时改这一篇的作者信息。
      </div>
    </NCard>

    <!-- 顺手给个去系统设置的入口：个人中心放的是「我」，系统层面的事在另一页
         （服务健康、AI 对话通道、公众号凭证、环境变量都在那边） -->
    <div class="to-settings">
      想改服务健康、AI 对话通道、公众号凭证、环境变量？去
      <a @click.prevent="router.push({ name: 'settings' })">系统设置</a>
    </div>
  </div>
</template>

<style scoped>
.profile-page {
  max-width: 860px;
}

/* 头像区：大圆预览 + 操作按钮，和下面的描述列表之间留口气 */
.avatar-row {
  display: flex;
  align-items: center;
  gap: 16px;
  margin-bottom: 18px;
}

.avatar-preview {
  width: 64px;
  height: 64px;
  border-radius: 50%;
  flex-shrink: 0;
  background: linear-gradient(135deg, var(--app-primary), var(--app-purple));
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 26px;
  font-weight: 600;
  color: #fff;
  object-fit: cover;
}

.avatar-actions {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.avatar-btns {
  display: flex;
  gap: 8px;
  align-items: center;
}

.api-base-hint {
  margin-top: 8px;
  font-size: 12px;
  color: var(--app-text-2);
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}

.field-note {
  font-size: 12px;
  color: var(--app-text-3);
}

/* 资料表单：标签左对齐，行距比作者信息那张卡松一点（信息密度低一些） */
.profile-form {
  margin-top: 14px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.form-row {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}

.form-label {
  width: 48px;
  flex-shrink: 0;
  font-size: 13px;
  color: var(--app-text-2);
  text-align: right;
}

.author-form {
  display: flex;
  gap: 8px;
  align-items: center;
  flex-wrap: wrap;
}

/* 我的配额（从设置页原样搬来） */
.quota-line {
  display: flex;
  align-items: baseline;
  gap: 10px;
  font-size: 13px;
  line-height: 1.9;
}

.quota-name {
  min-width: 72px;
  color: var(--app-text-2);
}

.quota-val {
  font-variant-numeric: tabular-nums;
}

.quota-val.danger {
  color: #d03050;
  font-weight: 600;
}

.quota-val.muted {
  color: var(--app-text-3);
}

.quota-period {
  font-size: 12px;
  color: var(--app-text-3);
}

.quota-note {
  margin-top: 8px;
  font-size: 12px;
  color: var(--app-text-3);
  line-height: 1.7;
}

.to-settings {
  margin-top: 8px;
  font-size: 12px;
  color: var(--app-text-3);
}

.to-settings a {
  color: var(--app-primary);
  cursor: pointer;
}
</style>
