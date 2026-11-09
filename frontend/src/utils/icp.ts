/**
 * ICP 备案号页脚（中国大陆境内公网部署才需要）。
 *
 * 备案号是该站点运营主体名下的私有信息，**不随开源代码分发**：
 * 仓库里只保留这个空壳组件，真实号由构建期环境变量 `VITE_ICP_BEIAN` 注入。
 *
 * 用法（在 `frontend/.env`，该文件已被 .gitignore 忽略）：
 *
 *   VITE_ICP_BEIAN=京ICP备00000000号-1
 *   # 可选，默认 https://beian.miit.gov.cn
 *   VITE_ICP_BEIAN_URL=https://beian.miit.gov.cn
 *
 * 未配置时函数静默返回，页脚保持 hidden——本地开发、境外部署、
 * 以及其他人 clone 开源版构建出来的站点都不会出现空白页脚或占位符。
 */

const DEFAULT_URL = 'https://beian.miit.gov.cn'

export function mountIcpFooter(): void {
  const code = (import.meta.env.VITE_ICP_BEIAN ?? '').trim()
  if (!code) return

  const host = document.getElementById('icp-foot')
  if (!host) return

  const link = document.createElement('a')
  link.href = (import.meta.env.VITE_ICP_BEIAN_URL ?? '').trim() || DEFAULT_URL
  link.target = '_blank'
  link.rel = 'noopener'
  link.textContent = code

  host.replaceChildren(link)
  host.hidden = false
}
