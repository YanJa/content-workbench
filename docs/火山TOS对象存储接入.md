# 火山 TOS 对象存储接入（STORAGE_BACKEND=s3）

> 一句话：把素材与排版配图从 ECS 本地盘挪到火山 TOS，浏览器**直连 TOS 公网 URL**，
> 图片流量不再经过 ECS 的 1M 带宽 —— 小带宽服务器上「浏览图片卡顿」的根治方案。

## 为什么

服务器（4C/4G、**1M 带宽**）走图片时，每张图都要从 ECS 的 1M 上行挤出来，
多图叠加会把 JS/API 一起拖慢。把图放到对象存储后，图片字节由 TOS 直出到浏览器，
ECS 只负责「业务 API + 上传/读回」，带宽压力几乎归零。

## 关键事实（踩过的坑，改配置前先读）

- **SecretAccessKey 是 base64 形态**（形如 `Wmpn...==`），**原样填即可，不要解码**。
  实测解码后反而认证失败。
- **内网端点**（`tos-s3-{region}.ivolces.com`）解析到 `100.64.x`（CGNAT 段），
  只有「同 VPC/同地域」的资源能连通。若 ECS 连不通（连接挂起），把
  `TOS_USE_INTERNAL=false`，API 改走公网端点 `tos-s3-{region}.volces.com`。
  上传是**入站流量（免费）**，只有浏览器读图会消耗赠送的「公网流出流量包」。
- **浏览器 URL 用「桶域名」**（`{bucket}.tos-{region}.volces.com`），与 S3 **接口端点**
  （`tos-s3-{region}.volces.com`）是两套域名，别混。浏览器走桶域名，SDK/接口走接口端点。
- 对象要 `public-read` 才能匿名直连；前提是桶**没开**「阻止公开访问」（Block Public Access）。
  本项目上传时自动带 `ACL=public-read`。

## 配置（backend/.env）

见 `backend/.env.example` 的「素材库 / 火山引擎 TOS」段，最小可用只要 6 个：

```env
STORAGE_BACKEND=s3
TOS_ACCESS_KEY=AKLT...           # 火山 AccessKey
TOS_SECRET_KEY=Wmpn...==         # 原样填，不要解码
TOS_BUCKET=cwb
TOS_REGION=cn-beijing
TOS_USE_INTERNAL=false           # ECS 连不通内网就填 false，走公网端点
TOS_ACL_PUBLIC=true
```

## 实现位置

- `backend/app/services/storage.py`
  - `StorageBackend` 契约；`LocalStorage`（本地盘 + HMAC 签名直链）与
    `TOSStorage`（TOS + 公网永久直链）两套实现，`get_storage()` 按 `STORAGE_BACKEND` 分派。
  - `gen_key(cid, fname)`：排版配图的 key（`gen/<cid>/<fname>`），与本地落点同构。
  - `put_gen()`：把排版配图字节镜像到云端（本地后端直接跳过）。
- **素材库**（`services/assets.py`）：走 `get_storage()`，切后端自动生效，前端零改动。
- **排版配图**（`routers/layout.py` / `services/layout.py`）：URL 走 `get_storage().url()`，
  写本地盘（微信转存仍读本地）的同时镜像到 TOS。
- **微信转存**（`services/wechat.py`）：扫描正文时兼容「本地直链」与「TOS 公网直链」两种 URL。

## 存量数据迁移

- **素材库**：URL 是读取时现算的，切 s3 后老素材的 URL 会指向 TOS。若老素材只在本地，
  需把它们**按同 key 拷到 TOS**（脚本思路：遍历 `assets` 表，读 `UPLOAD_DIR/<storage_key>`
  上传到 TOS 同 key，再把 `storage` 字段改为 `s3`）。
- **排版配图（gen/）**：老内容的图仍保留本地签名直链（`serve_image` 从本地盘读），
  新生成内容才走 TOS，互不影响；微信推送两种 URL 都能转存。

## 交付验证

```bash
# 1. 上传一个 public-read 对象
# 2. 用浏览器/ curl 匿名拉公网 URL，应返回 200
curl -o /dev/null -w "%{http_code}\n" "https://cwb.tos-cn-beijing.volces.com/<key>"
```

## 资源包口径（别混淆）

火山赠送的三个包是三个独立维度：
- **容量包 10GiB** = 桶里能存多少（存多大）；
- **公网流出流量包 10GB** = 浏览器从 TOS 读走多少（运出多少），10GB ≈ 几万次 300KB 图；
- **请求包 20 万次** = 操作次数（put/get/delete 计数）。

同地域 ECS↔TOS 走内网时**不消耗**公网流出包。
