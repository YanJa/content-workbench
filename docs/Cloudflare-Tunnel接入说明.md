# Cloudflare Tunnel 接入说明

> 2026-10-15 实施。解决「不翻墙无法访问 `https://cwb.draftpaper.ltd`」的问题。

## 一、问题背景

### 现象

* 本地访问 `https://cwb.draftpaper.ltd` 报 `curl: (35) Recv failure: Connection was reset`
* 必须开代理（美国 VPS 出口）才能访问
* 服务器、DNS、证书、隧道**全都正常**，第三方网络（沙箱）访问一切正常

### 根因

**本地网络到 `49.233.223.252:443` 的 TLS 握手被中间设备 RST 阻断。**

判断依据（curl -v 输出）：

```
* IPv4: 49.233.223.252          ← DNS 解析正常
* Trying 49.233.223.252:443...  ← TCP 能连上
* Recv failure: Connection was reset   ← TLS 握手阶段被 RST
```

逐层排查结论：

| 层 | 状态 | 证据 |
|---|---|---|
| DNS | ✅ 正常 | 权威 NS + 公共 DNS 都返回 `49.233.223.252`，无 AAAA 干扰 |
| TCP | ✅ 能连 | `Trying...` 之后没有超时 |
| TLS | ❌ 被 RST | 握手阶段连接重置 |
| 服务器 443 | ✅ 正常 | nginx 监听 `0.0.0.0:443`，TLS 配置标准，无握手错误日志 |
| 证书 | ✅ 有效 | Let's Encrypt，`CN=cwb.draftpaper.ltd`，有效期至 2027-01-12 |
| 第三方访问 | ✅ 通 | 沙箱三种方式（IP 直连 / SNI / --resolve）全返回 401 |

典型 **SNI 阻断**特征：TCP 层放行，TLS ClientHello（含 SNI 域名）一发出就被 reset。

### 为什么开橙云没用（踩过的坑）

按常规思路把 Cloudflare DNS 改成橙云代理后，**反而报 525 SSL handshake failed**：

```
Error 525: SSL handshake failed
The SSL/TLS handshake between Cloudflare and the origin server failed.
```

原因：**CF 的边缘节点在境外，它回源境内 IP 时撞上了同一堵墙。**

服务器日志证实了这一点——`grep` Cloudflare 的 IP 段，命中数 **0**，CF 边缘节点压根没连到源站。

> 这是个死结：橙云解决了「你→CF」的阻断，但「CF→源站」又遇到同样的阻断。
> 解法只能让源站**主动连出去**，即 Cloudflare Tunnel。

## 二、最终架构

```
你（任意网络）
   ↓ HTTPS
Cloudflare 边缘节点（就近接入，如 AMS/LAX/LHR）
   ↓ Cloudflare Tunnel（QUIC，源站主动拨出）
腾讯云服务器 49.233.223.252
   ↓ cloudflared 回源 https://127.0.0.1:443（带 originServerName）
nginx（cwb 站点：basic auth + SSE/WebSocket 调优）
   ↓ proxy_pass http://127.0.0.1:18080
SSH 反向隧道（服务器 18080 → 沙箱 5173）
   ↓
沙箱：Vite 前端(5173) + FastAPI 后端(8000)
```

**关键点**：隧道由源站**主动拨出**到 CF，不存在「CF 连不进来」的问题，彻底绕过跨境 TLS 阻断。

## 三、服务器侧配置

### 3.1 安装 cloudflared

**不要**从 GitHub Release 下载 deb —— 实测在国内服务器上被限速截断（3.4MB / 实际 19MB），`dpkg -i` 报 `archive is truncated or corrupt`。

用官方 apt 源：

```bash
sudo mkdir -p --mode=0755 /usr/share/keyrings
curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg | \
    sudo tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null
echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main" | \
    sudo tee /etc/apt/sources.list.d/cloudflared.list
sudo apt-get update
sudo apt-get install -y cloudflared
```

### 3.2 登录授权

```bash
cloudflared tunnel login
```

会输出一个 `dash.cloudflare.com/argotunnel?...` 链接，浏览器打开并选中 **draftpaper.ltd** 授权。
授权成功后 `~/.cloudflared/cert.pem` 自动下载。

### 3.3 创建隧道

```bash
cloudflared tunnel create cwb
```

产出的信息（本项目实际值）：

| 项 | 值 |
|---|---|
| 隧道名 | `cwb` |
| 隧道 ID | `ac558841-1248-4a72-be90-c21a8dbdcb29` |
| 凭证文件 | `/home/ubuntu/.cloudflared/ac558841-1248-4a72-be90-c21a8dbdcb29.json` |

### 3.4 DNS 记录

`cloudflared tunnel route dns` **只能创建、不能覆盖**。如果同名 A 记录已存在会报：

```
code: 1003, Failed to create record ... An A, AAAA, or CNAME record with that host already exists.
```

**必须在 Cloudflare 控制台先删/改掉旧记录**，然后二选一：

* 手动改：DNS → `cwb` 记录 → 类型改 **CNAME**，目标填
  `ac558841-1248-4a72-be90-c21a8dbdcb29.cfargotunnel.com`，保持**橙云**
* 或删掉旧记录后执行 `cloudflared tunnel route dns cwb cwb.draftpaper.ltd`

> 注意：橙云模式下 `dig` 查到的永远是 CF 的边缘 IP（`104.21.x` / `172.67.x`），
> 这是正常的，CNAME 只在 CF 内部生效。

### 3.5 配置文件

`~/.cloudflared/config.yml`：

```yaml
tunnel: ac558841-1248-4a72-be90-c21a8dbdcb29
credentials-file: /home/ubuntu/.cloudflared/ac558841-1248-4a72-be90-c21a8dbdcb29.json

loglevel: info
metrics: 127.0.0.1:20241

ingress:
  - hostname: cwb.draftpaper.ltd
    service: http://127.0.0.1:18080
    originRequest:
      disableChunkedEncoding: false
      connectTimeout: 30s
      noTLSVerify: true
  - service: http_status:404
```

### 3.6 装成系统服务

```bash
sudo cloudflared --config /home/ubuntu/.cloudflared/config.yml service install
```

`install` 会把配置复制到 `/etc/cloudflared/config.yml`，服务以 **root** 身份运行
（`/etc/systemd/system/cloudflared.service`）。

验证：

```bash
sudo systemctl is-enabled cloudflared   # enabled
sudo systemctl is-active cloudflared    # active
sudo journalctl -u cloudflared -n 50 | grep "Registered tunnel connection"
```

正常会看到 **3~4 条** `Registered tunnel connection`（connIndex=0/1/2/3）。
偶有 1 条 QUIC 拨号超时（`Failed to dial a quic connection`）不影响服务，其余连接可用。

## 四、运维命令

```bash
# 查状态
sudo systemctl status cloudflared
sudo journalctl -u cloudflared -f

# 重启
sudo systemctl restart cloudflared

# 查隧道信息
cloudflared tunnel list
cloudflared tunnel info cwb

# 卸载服务
sudo cloudflared service uninstall
```

> **链路延迟说明**：这条链路每一跳都是跨境的（浏览器 → CF 边缘 → Tunnel →
> 服务器 nginx → SSH 反向隧道 → 沙箱），单次请求实测 **1.7~2.9 秒**。
> 接口本身在后端只要几毫秒，慢的是链路。
>
> 所以**别用大响应的接口做高频轮询**：渲染任务轮询要用
> `/api/video/jobs/{id}/progress`（267 字节），不要用完整详情（22KB）。
> 详见 `API接口文档.md` 的相关说明。

## 五、回源配置：走 nginx（当前生效）

> **2026-10-15 更新**：最初实施时曾直连 `127.0.0.1:18080` 绕过 nginx，
> 但那样会丢掉 basic auth、站点变公开。**现已改回走 nginx 443**，认证已恢复。

### 5.1 当前配置（`/etc/cloudflared/config.yml`）

```yaml
tunnel: ac558841-1248-4a72-be90-c21a8dbdcb29
credentials-file: /home/ubuntu/.cloudflared/ac558841-1248-4a72-be90-c21a8dbdcb29.json

loglevel: info
metrics: 127.0.0.1:20241

ingress:
  # 回源到本机 nginx 443（保留 basic auth + SSE/WebSocket 调优）
  - hostname: cwb.draftpaper.ltd
    service: https://127.0.0.1:443
    originRequest:
      # 关键：告诉 nginx 用的是哪个域名，否则匹配不到 cwb 站点
      originServerName: cwb.draftpaper.ltd
      # 源站是回环访问，跳过证书域名校验
      noTLSVerify: true
      connectTimeout: 30s
      disableChunkedEncoding: false
  - service: http_status:404
```

### 5.2 两个关键参数（漏了会出问题）

**`originServerName: cwb.draftpaper.ltd`（必须）**

cloudflared 回源到 `https://127.0.0.1:443` 时，如果不显式指定 SNI，
nginx 收到的是 IP 直连请求，匹配不到 `server_name cwb.draftpaper.ltd` 的站点，
会落到 `default` 站点（返回默认页或 404）。这个参数就是补上 SNI。

**`noTLSVerify: true`**

回环访问时用 IP 连接、但证书是签给域名的，严格校验会失败。
这是本机内部通信（127.0.0.1），跳过校验风险可控。

### 5.3 架构对比

| | 直连 18080（已弃用） | 走 nginx 443（当前） |
|---|---|---|
| 回源路径 | 隧道 → 18080 → 沙箱 | 隧道 → nginx 443 → 18080 → 沙箱 |
| basic auth | ❌ 失效，站点公开 | ✅ 生效（用户名 `cwb`） |
| SSE 调优 | 靠 cloudflared 默认 | ✅ nginx 的 `proxy_buffering off` 生效 |
| 跳数 | 少一跳 | 多一跳（实测无感，1.2~1.7s） |
| 证书 | 无（明文 HTTP） | ✅ 端到端加密 |

验证方式（401 + 认证头 = nginx 生效）：

```bash
curl -sI https://cwb.draftpaper.ltd/ | grep -i "www-authenticate"
# www-authenticate: Basic realm="Content Workbench - Authorized Only"
```

> 判断回源是否真的走了 nginx：看服务器 `cwb-access.log`，
> 客户端 IP 会显示为 `127.0.0.1`（cloudflared 从本机连入）。

### 5.4 SSE / WebSocket

走 nginx 后，原有 nginx 配置全部生效：

* **SSE 逐字输出**：靠 nginx 的 `proxy_buffering off` + `proxy_read_timeout 3600s`
* **WebSocket（Vite HMR）**：靠 nginx 的 `Upgrade` / `Connection` 头透传

cloudflared 本身也**不缓冲**流式响应、**透传 WebSocket Upgrade**，
两层都不阻塞，所以对话页的流式输出和 HMR 都正常。

### 5.5 修改 basic auth 密码

站点用 nginx `auth_basic` 保护，账号 `cwb`，密码存在 `/etc/nginx/.htpasswd`。
改密码**不要试图装 `apache2-utils`**——腾讯云服务器上 `mirrors.tencentyun.com`
经常解析失败，装了白折腾。直接用 Python 的 `passlib` 生成哈希：

```bash
python3 -c "
from passlib.hash import apr_md5_crypt
print('cwb:' + apr_md5_crypt.using(salt='wbCwb26').hash('新密码'))
"
```

把输出整行写进 `/etc/nginx/.htpasswd`：

```bash
sudo cp /etc/nginx/.htpasswd /etc/nginx/.htpasswd.bak-$(date +%Y%m%d-%H%M%S)
echo 'cwb:$apr1$wbCwb26$Qf7Sm/YbRikKmiSwpgYFg1' | sudo tee /etc/nginx/.htpasswd >/dev/null
sudo chown root:www-data /etc/nginx/.htpasswd
sudo chmod 640 /etc/nginx/.htpasswd
```

`.htpasswd` 是**每次请求实时读取**的，**不需要 reload nginx**。

验证：

```bash
curl -s -o /dev/null -w "%{http_code}\n" -u "cwb:新密码" https://cwb.draftpaper.ltd/   # 期望 200
curl -s -o /dev/null -w "%{http_code}\n" -u "cwb:错密码" https://cwb.draftpaper.ltd/   # 期望 401
```

> **踩坑记录**：apr1（`$apr1$`）的 salt **最多 8 个字符**。
> 一开始用了 10 字符的 salt（`wbCwb2026`），哈希写进去后正确密码也返回 401，
> nginx 报 `user "cwb": password mismatch`。换成 8 字符（`wbCwb26`）后立即正常。
> 另外 `$1$`（标准 MD5-crypt）和 `$apr1$` 虽然算法主体相同，但 magic 字符串不同，
> nginx **只认 `$apr1$`**，别用 `crypt.crypt()` 生成。

### 5.6 如需改回直连（不推荐）

```bash
sudo tee /etc/cloudflared/config.yml >/dev/null <<'EOF'
tunnel: ac558841-1248-4a72-be90-c21a8dbdcb29
credentials-file: /home/ubuntu/.cloudflared/ac558841-1248-4a72-be90-c21a8dbdcb29.json
loglevel: info
metrics: 127.0.0.1:20241
ingress:
  - hostname: cwb.draftpaper.ltd
    service: http://127.0.0.1:18080
  - service: http_status:404
EOF
sudo systemctl restart cloudflared
```

备份文件在 `/etc/cloudflared/config.yml.bak-*`，可直接还原。


* 对话页的 SSE 逐字输出 ✅ 正常
* Vite HMR 的 WebSocket ✅ 正常

已在配置里设了 `disableChunkedEncoding: false` 和 30s `connectTimeout`。

### 5.3 仍然依赖沙箱 SSH 反向隧道

隧道回源的 `127.0.0.1:18080` 是**服务器上的 SSH 反向隧道端口**，它指向沙箱的 5173。
所以完整链路仍然依赖：

```
沙箱服务存活 → SSH 反向隧道存活 → 18080 有响应 → cloudflared 才能回源
```

**沙箱休眠重启后**，需要：

```bash
bash deploy/install-autostart.sh    # 重建沙箱内的 supervisord 托管（含 tunnel-guard）
```

`tunnel-guard.sh` 会在沙箱内自动重连 SSH 反向隧道，`cloudflared` 是服务器侧独立服务，
不受沙箱影响。

## 六、故障排查速查

| 现象 | 检查 | 处理 |
|---|---|---|
| 页面 502/503 | 服务器 `curl 127.0.0.1:18080` | 沙箱服务或 SSH 隧道断了，跑 `install-autostart.sh` |
| 页面 525 | `sudo journalctl -u cloudflared` | 隧道没跑，`sudo systemctl restart cloudflared` |
| 页面 1033 | `cloudflared tunnel list` 看 CONNECTIONS | 隧道无活跃连接，重启服务 |
| 本地完全不解析 | `dig +short cwb.draftpaper.ltd` | DNS 记录被改，检查 CF 控制台 |
| 隧道起不来 | `cloudflared tunnel info cwb` | 凭证文件丢失/权限问题，重跑 `tunnel login` + `create` |
