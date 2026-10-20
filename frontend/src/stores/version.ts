/**
 * 版本 Store：管「当前版本」与「用户已看过的版本」，驱动两件事：
 *
 * 1. **新功能提醒**：拿到后端版本后，和本地记录的「已看版本」比较，
 *    若升过级就自动弹出「新功能」卡片（只看比已看版本更新的那几条）；
 *    用户看完/关闭后把已看版本更新成当前版，下次同版本不再弹。
 * 2. **更新日志入口**：随时手动打开，看全部历史（不受已看版本影响）。
 *
 * 持久化沿用 onboarding 的范式：localStorage（cwb_version_seen），
 * 隐私模式下读写 try/catch 吞掉，读失败按「没看过」处理。
 *
 * 种子逻辑（版本功能上线时的兼容处理）：
 * - 本地没记录过版本：
 *   - 新用户（引导还没看过）→ 当前版当基线，不弹（交给新手指引）；
 *   - 老用户（引导已看过）→ 视为从上一版（1.0.0）升级，种入旧版号，
 *     以便本次 1.1.0 的「新功能」能正常弹出。
 * - 本地有记录且与当前版不同 → 升过级，弹新功能。
 */
import { computed, ref } from 'vue'
import { defineStore } from 'pinia'

import { APP_VERSION, CHANGELOG, type ChangelogEntry } from '@/version/changelog'
import { useOnboardingStore } from '@/stores/onboarding'

const SEEN_KEY = 'cwb_version_seen'
/** 版本功能上线前的内部版本，作为老用户的「上一版」种子 */
const LEGACY_VERSION = '1.0.0'

function readSeen(): string {
  try {
    return localStorage.getItem(SEEN_KEY) || ''
  } catch {
    return ''
  }
}

function writeSeen(v: string): void {
  try {
    localStorage.setItem(SEEN_KEY, v)
  } catch {
    /* 存不进去不影响本次会话 */
  }
}

/** 语义化版本比较：a>b 返 1，a<b 返 -1，相等返 0（按 major.minor.patch 数值比）。 */
export function compareVersion(a: string, b: string): number {
  const pa = a.split('.').map(Number)
  const pb = b.split('.').map(Number)
  for (let i = 0; i < 3; i++) {
    const x = pa[i] || 0
    const y = pb[i] || 0
    if (x !== y) return x > y ? 1 : -1
  }
  return 0
}

export const useVersionStore = defineStore('version', () => {
  /** 后端返回的真实当前版本 */
  const current = ref('')
  /** 本地记录的「已看版本」 */
  const seen = ref(readSeen())
  /** 「新功能」卡片是否打开（版本升级后自动弹） */
  const whatsNewOpen = ref(false)
  /** 「更新日志」全量弹窗是否打开（手动入口） */
  const changelogOpen = ref(false)

  /** 全部版本记录（从新到旧），手动更新日志用 */
  const allEntries = computed<ChangelogEntry[]>(() => CHANGELOG)
  /** 比「已看版本」更新的记录，自动新功能提醒用 */
  const newEntries = computed<ChangelogEntry[]>(() =>
    seen.value ? CHANGELOG.filter((e) => compareVersion(e.version, seen.value) > 0) : [],
  )

  /** 收到后端版本后初始化：决定要不要弹「新功能」。
   *
   *  version 为空（后端旧代码没返回 / 探活失败）时兜底用前端 APP_VERSION，
   *  保证 current 一定有值、关于卡片不会只剩一个 "v"。后端一旦返回
   *  version，则以后端为准。 */
  function init(version?: string): void {
    const v = version || APP_VERSION
    current.value = v
    const s = readSeen()
    if (!s) {
      // 没记录过：新用户（引导没看过）把当前版当基线；老用户种入旧版号。
      const baseline = useOnboardingStore().done ? LEGACY_VERSION : v
      writeSeen(baseline)
      seen.value = baseline
      maybeShowWhatsNew()
    } else {
      seen.value = s
      if (s !== v) maybeShowWhatsNew()
    }
  }

  function maybeShowWhatsNew(): void {
    if (newEntries.value.length > 0) whatsNewOpen.value = true
  }

  /** 关闭「新功能」：把已看版本推进到当前版，下次同版本不再弹。 */
  function dismissWhatsNew(): void {
    whatsNewOpen.value = false
    if (current.value) {
      writeSeen(current.value)
      seen.value = current.value
    }
  }

  function openChangelog(): void {
    changelogOpen.value = true
  }

  function closeChangelog(): void {
    changelogOpen.value = false
  }

  return {
    current,
    seen,
    whatsNewOpen,
    changelogOpen,
    allEntries,
    newEntries,
    init,
    dismissWhatsNew,
    openChangelog,
    closeChangelog,
  }
})
