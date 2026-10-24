/**
 * 模型池接口。
 *
 * 后端约定（backend/app/routers/model_pool.py）：
 *   - 读端点登录即可，**写端点只有管理员能调**；
 *   - 所有 api_key 一律脱敏返回（前 4 后 4），拿不到明文；
 *   - 换 embedding 模型会撞 409（code = EMBED_DIM_MISMATCH），detail 是
 *     一个对象而不是字符串，这里要能原样透出去给页面做二次确认。
 */
import { http } from './http'
import type {
  ModelBinding,
  ModelCatalogEntry,
  ModelFeature,
  ModelProviderRow,
  MyProviderKey,
  PoolMeta,
  QuotaRow,
  SyncCandidate,
} from '@/types'

/** 8 个功能槽的中文名 + 默认任务档位 + 是否禁备选。 */
export async function poolMeta(): Promise<PoolMeta> {
  const { data } = await http.get('/model-pool/features')
  return data
}

// ---------------------------------------------------------------- 服务商

export async function listProviders(): Promise<ModelProviderRow[]> {
  const { data } = await http.get('/model-pool/providers')
  return data.items ?? []
}

export interface ProviderPayload {
  key: string
  name?: string
  kind?: 'openai' | 'cloudflare'
  base_url?: string
  api_key?: string
  enabled?: boolean
  note?: string
  daily_calls_limit?: number | null
  daily_token_limit?: number | null
  monthly_token_limit?: number | null
  rpm_limit?: number | null
  quota_reset_tz?: 'cst' | 'utc'
  /** 这家服务商的模型默认算什么账：模型自己没标 billing 时用它兜底。
   *  整家都是免费档（Cloudflare、火山免费额度……）选 free，记账就是 0 元。 */
  billing_default?: 'free' | 'paid'
}

export async function createProvider(payload: ProviderPayload): Promise<ModelProviderRow> {
  const { data } = await http.post('/model-pool/providers', payload)
  return data.item
}

export async function updateProvider(
  key: string,
  payload: Partial<ProviderPayload>,
): Promise<ModelProviderRow> {
  const { data } = await http.patch(`/model-pool/providers/${key}`, payload)
  return data.item
}

export async function deleteProvider(key: string): Promise<number> {
  const { data } = await http.delete(`/model-pool/providers/${key}`)
  return data.deleted_models ?? 0
}

export interface TestResult {
  ok: boolean
  count: number
  sample: string[]
}

/** 连通性测试：拉一次模型列表，不烧额度。可传临时 api_key 先试再存。 */
export async function testProvider(key: string, apiKey = ''): Promise<TestResult> {
  const { data } = await http.post(`/model-pool/providers/${key}/test`, {
    api_key: apiKey,
  })
  return data
}

/** 拉远端模型清单（不自动入库，返回候选供勾选）。 */
export async function syncProvider(key: string): Promise<SyncCandidate[]> {
  const { data } = await http.post(`/model-pool/providers/${key}/sync`)
  return data.items ?? []
}

// ---------------------------------------------------------------- 模型

export async function listCatalog(): Promise<ModelCatalogEntry[]> {
  const { data } = await http.get('/model-pool/catalog')
  return data.items ?? []
}

export interface CatalogPayload {
  provider_key: string
  model_id: string
  label?: string
  capabilities?: string[]
  protocol?: string
  dimensions?: number | null
  billing?: 'free' | 'paid'
  supports_tools?: boolean
  supports_stream_usage?: boolean
  quota_scope?: 'model' | 'provider'
  daily_calls_limit?: number | null
  daily_token_limit?: number | null
  monthly_token_limit?: number | null
  rpm_limit?: number | null
  enabled?: boolean
  note?: string
}

export async function createCatalog(payload: CatalogPayload): Promise<ModelCatalogEntry> {
  const { data } = await http.post('/model-pool/catalog', payload)
  return data.item
}

export async function bulkCreateCatalog(
  providerKey: string,
  items: { model_id: string; label?: string; capabilities?: string[]; protocol?: string }[],
): Promise<number> {
  const { data } = await http.post('/model-pool/catalog/bulk', {
    provider_key: providerKey,
    items,
  })
  return data.added ?? 0
}

export async function updateCatalog(
  id: string,
  payload: Partial<CatalogPayload> & { disabled_until?: number; sort?: number },
): Promise<ModelCatalogEntry> {
  const { data } = await http.patch(`/model-pool/catalog/${id}`, payload)
  return data.item
}

export async function deleteCatalog(id: string): Promise<string[]> {
  const { data } = await http.delete(`/model-pool/catalog/${id}`)
  return data.affected_features ?? []
}

// ---------------------------------------------------------------- 绑定

export async function listBindings(): Promise<ModelBinding[]> {
  const { data } = await http.get('/model-pool/bindings')
  return data.items ?? []
}

/**
 * 保存某个功能槽的绑定。
 *
 * 换 embedding 模型会撞 409，detail 是对象：
 * `{code:'EMBED_DIM_MISMATCH', stale_chunks, from_model, to_model, message}`。
 * 这里**不吞**这个错误，原样抛给页面——页面要拿 stale_chunks 做二次确认。
 */
export async function saveBinding(
  feature: string,
  primary: string,
  fallbacks: string[],
  force = false,
): Promise<ModelBinding> {
  const { data } = await http.put(`/model-pool/bindings/${feature}`, {
    primary,
    fallbacks,
    force,
  })
  return data
}

// ---------------------------------------------------------------- 额度

export async function poolQuota(): Promise<QuotaRow[]> {
  const { data } = await http.get('/model-pool/quota')
  return data.items ?? []
}

// ---------------------------------------------------------------- 个人凭证

export async function listMyKeys(): Promise<MyProviderKey[]> {
  const { data } = await http.get('/model-pool/my-keys')
  return data.items ?? []
}

/** 传空串 = 删除覆盖、回退到平台全局 Key。 */
export async function saveMyKey(providerKey: string, apiKey: string): Promise<boolean> {
  const { data } = await http.put(`/model-pool/my-keys/${providerKey}`, { api_key: apiKey })
  return Boolean(data.using_my_key)
}

export async function verifyMyKey(providerKey: string): Promise<number> {
  const { data } = await http.post(`/model-pool/my-keys/${providerKey}/verify`)
  return data.count ?? 0
}

// ---------------------------------------------------------------- 厂商模板

export interface VendorTemplate {
  key: string
  name: string
  base_url: string
  kind: 'openai' | 'cloudflare'
  /** 日额度在哪儿重置：CF 与 OpenRouter 是 00:00 UTC，国内厂商按北京时间 */
  tz: 'cst' | 'utc'
  hint: string
}

/**
 * 常见厂商的 OpenAI 兼容端点。选了只填 Key 就能用，不用去翻文档。
 *
 * 这些地址 2026-10 逐条核对过（两家独立来源一致）：官方文档 + 百度智能云
 * 公开的「导入其他厂商模型服务」对照表。厂商改了地址的话，页面上还能手改。
 */
export const VENDOR_TEMPLATES: VendorTemplate[] = [
  {
    key: 'zhipu',
    name: '智谱 GLM（GLM-4-Flash 系列永久免费）',
    base_url: 'https://open.bigmodel.cn/api/paas/v4',
    kind: 'openai',
    tz: 'cst',
    hint: '免费档约 30 QPS；embedding-3 也有免费额度',
  },
  {
    key: 'siliconflow',
    name: '硅基流动（≤9B 模型永久免费）',
    base_url: 'https://api.siliconflow.cn/v1',
    kind: 'openai',
    tz: 'cst',
    hint: '一家接 200+ 模型；免费档限小参数模型',
  },
  {
    key: 'volcengine',
    name: '火山方舟（新模型 200 万 token/天）',
    base_url: 'https://ark.cn-beijing.volces.com/api/v3',
    kind: 'openai',
    tz: 'cst',
    hint: '免费额度按天刷新，是账号级共享',
  },
  {
    key: 'dashscope',
    name: '阿里百炼（7000 万 token / 90 天）',
    base_url: 'https://dashscope.aliyuncs.com/compatible-mode/v1',
    kind: 'openai',
    tz: 'cst',
    hint: '开通后 90 天内有效，注意过期',
  },
  {
    key: 'qianfan',
    name: '百度千帆（ERNIE-Speed/Lite 永久免费）',
    base_url: 'https://qianfan.baidubce.com/v2',
    kind: 'openai',
    tz: 'cst',
    hint: '免费档 QPS 50，是这批里最宽松的',
  },
  {
    key: 'hunyuan',
    name: '腾讯混元（Hunyuan-lite 永久免费）',
    base_url: 'https://api.hunyuan.cloud.tencent.com/v1',
    kind: 'openai',
    tz: 'cst',
    hint: '需在控制台开通后才能调通',
  },
  {
    key: 'spark',
    name: '讯飞星火（Spark Lite 永久免费）',
    base_url: 'https://spark-api-open.xf-yun.com/v1',
    kind: 'openai',
    tz: 'cst',
    hint: '免费档 QPS 只有 2，建议搭配 RPM 限制用',
  },
  {
    key: 'moonshot',
    name: 'Moonshot / Kimi',
    base_url: 'https://api.moonshot.cn/v1',
    kind: 'openai',
    tz: 'cst',
    hint: '付费为主，可选',
  },
  {
    key: 'minimax',
    name: 'MiniMax',
    base_url: 'https://api.minimaxi.com/v1',
    kind: 'openai',
    tz: 'cst',
    hint: '付费为主，可选',
  },
  {
    key: 'openrouter',
    name: 'OpenRouter（聚合，约 50 请求/天免费）',
    base_url: 'https://openrouter.ai/api/v1',
    kind: 'openai',
    tz: 'utc',
    hint: '免费额度按 UTC 重置；能一家接遍全球模型',
  },
]

/** 协议 → 中文（表格里显示用）。 */
export const PROTOCOL_LABEL: Record<string, string> = {
  openai_chat: '对话（OpenAI 兼容）',
  openai_images: '生图（OpenAI 兼容）',
  openai_embeddings: '向量化（OpenAI 兼容）',
  cf_ai_run_image: '生图（Cloudflare REST）',
  cf_ai_run_embed: '向量化（Cloudflare REST）',
}

/** 能力 → 中文。 */
export const CAPABILITY_LABEL: Record<string, string> = {
  chat: '对话',
  vision: '视觉',
  image: '生图',
  embedding: '向量',
}

export function featureLabel(f: ModelFeature): string {
  return f.name
}
