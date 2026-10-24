/** 技能包（Skill）接口。
 *
 * 概念：一个技能包就是一份 SKILL.md，开头是 YAML frontmatter（name +
 * description），后面是正文。它不会一直占着上下文 —— 平时只把 name +
 * description 作为「清单」告诉模型，模型觉得匹配才调 activate_skill 取正文。
 */
import { http } from './http'

export interface Skill {
  id: string
  name: string
  description: string
  /** SKILL.md 正文（去掉 frontmatter） */
  body: string
  enabled: boolean
  /** ready（可用）/ invalid（解析失败，error 里是原因） */
  status: 'ready' | 'invalid'
  error: string
  use_count: number
  created_at: number
  updated_at: number
  /** zip 上传时带进来的附件路径列表（后端只回 path，不回内容） */
  files?: string[]
}

export interface SkillsMeta {
  enabled: boolean
  catalog_max: number
  body_max_chars: number
}

export async function listSkills(): Promise<Skill[]> {
  const { data } = await http.get('/skills')
  return data ?? []
}

export async function skillsMeta(): Promise<SkillsMeta> {
  const { data } = await http.get('/skills/meta')
  return data
}

/** 内置技能（随代码发布，只读：没有 id、不能删、不能停用） */
export interface BuiltinSkill {
  name: string
  description: string
}

export async function listBuiltinSkills(): Promise<BuiltinSkill[]> {
  const { data } = await http.get('/skills/builtins')
  return data?.items ?? []
}

/** 粘贴 / 手填录入（正文里必须带 frontmatter）。同名覆盖。 */
export async function createSkill(text: string): Promise<Skill> {
  const { data } = await http.post('/skills', { text })
  return data
}

/** 上传 SKILL.md。文件名会参与 name 校验（等价于「name 必须等于父目录名」）。 */
export async function uploadSkill(file: File): Promise<Skill> {
  const form = new FormData()
  form.append('file', file)
  // 不手动设 Content-Type：axios 对 FormData 会自动带 multipart 边界
  const { data } = await http.post('/skills/upload', form)
  return data
}

/** 从 URL 导入技能（.md 或 .zip，如 https://developer.zhihu.com/download/hot_list_skills.zip）。 */
export async function importSkillUrl(url: string): Promise<Skill> {
  const { data } = await http.post('/skills/import_url', { url })
  return data
}

export async function patchSkill(
  id: string,
  patch: { enabled?: boolean; name?: string; text?: string },
): Promise<Skill> {
  const { data } = await http.patch(`/skills/${id}`, patch)
  return data
}

export async function deleteSkill(id: string): Promise<void> {
  await http.delete(`/skills/${id}`)
}

/** 一个能直接用、也值得参考的示例（前端「用示例填一下」按钮用）。 */
export const SKILL_EXAMPLE = `---
name: wechat-hot-title
description: 起公众号爆款标题。当用户要起标题、改标题、觉得现有标题太平淡、或需要更吸引点击的标题时使用。
---

# 公众号爆款标题

## 结构
- 用具体数字开头（3 个 / 90% / 第 2 天），不要用「一些」「几个」
- 前 10 个字必须出现利益点或冲突点，因为列表页只露这么多
- 长度控制在 16-24 字，手机上不超过两行

## 语气
- 说人话，不用「赋能」「闭环」「抓手」这类词
- 可以有情绪，但不能标题党到与正文不符

## 输出
一次给 5 个候选，每个后面用一句话说明它靠什么吸引点击。
不要解释方法本身，直接给标题。
`
