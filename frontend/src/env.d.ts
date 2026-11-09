/// <reference types="vite/client" />

declare module '*.vue' {
  import type { DefineComponent } from 'vue'
  const component: DefineComponent<object, object, unknown>
  export default component
}

interface ImportMetaEnv {
  readonly VITE_API_BASE: string
  /** ICP 备案号，仅境内公网部署需要；开源版留空（见 src/utils/icp.ts） */
  readonly VITE_ICP_BEIAN?: string
  /** 备案号查询链接，缺省 https://beian.miit.gov.cn */
  readonly VITE_ICP_BEIAN_URL?: string
  /** 邀请文案里的注册直链；留空则用当前站点域名 */
  readonly VITE_INVITE_URL?: string
}
interface ImportMeta {
  readonly env: ImportMetaEnv
}
