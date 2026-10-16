/**
 * 新手引导 Store：只记住「用户是否已经走过一遍」。
 *
 * 沿用 stores/theme.ts 的持久化范式：
 * - 存 localStorage（cwb_onboarding_done），刷新不丢；
 * - 隐私模式下 localStorage 可能抛异常，一律 try/catch 吞掉，
 *   读失败按「没走过」处理（最多多弹一次，不影响使用）。
 *
 * 语义：只要用户**看完**或**主动跳过**，就算走过了，之后永不再自动弹；
 * 想看可以点侧栏/头部的「新手引导」按钮手动重看（reset 一下）。
 */
import { defineStore } from 'pinia'
import { ref } from 'vue'

const STORAGE_KEY = 'cwb_onboarding_done'
const DONE_VALUE = '1'

function readDone(): boolean {
  try {
    return localStorage.getItem(STORAGE_KEY) === DONE_VALUE
  } catch {
    // 隐私模式 / 禁用了存储：当作没走过
    return false
  }
}

export const useOnboardingStore = defineStore('onboarding', () => {
  const done = ref<boolean>(readDone())

  /** 标记为已走过（看完或跳过时调用），此后不再自动弹。 */
  function markDone(): void {
    done.value = true
    try {
      localStorage.setItem(STORAGE_KEY, DONE_VALUE)
    } catch {
      // 存不进去也不影响本次会话（内存里的 done 已置真）
    }
  }

  /** 清掉标记（手动重看引导时调用）。 */
  function reset(): void {
    done.value = false
    try {
      localStorage.removeItem(STORAGE_KEY)
    } catch {
      // 清不掉也不阻塞重看
    }
  }

  return { done, markDone, reset }
})
