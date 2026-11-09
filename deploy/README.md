# 部署手册（uv 方案）

## 沙箱自启动（先看这个）

`install-autostart.sh` 把 backend / frontend / tunnel-guard / proxy-guard 挂到**平台 supervisord** 上：

```bash
bash deploy/install-autostart.sh             # 安装并启动（幂等）
bash deploy/install-autostart.sh --uninstall # 停止并移除
```

托管之后进程崩溃会被自动拉起（已实测：`kill -9` 后端，10 秒内自愈）。

**但沙箱休眠重启后会失效，需要重新执行一次安装命令。** 原因（实测）：

| 想做的事 | 结果 |
|---|---|
| 把 `cwb.conf` 写进平台配置目录 `/.PlnPyKFp4CRfFtgC1/supervisord-conf/` | ❌ 该卷是 **只读挂载**（ext4 ro） |
| 改启动钩子 `workspace-init/init`（它启动时把 vs-code.conf 拷进 tmpfs） | ❌ 同上，只读 |
| 只写进 include 目录 `${IDE_EDITOR_SERVER_DIR}_run/supervisord-conf` | ⚠️ 生效，但那是 **tmpfs（10MB）**，容器每次启动重建 |

所以「真·开机自启」在当前沙箱形态下做不到，退而求其次：
**一次命令重建全部托管关系**，比手动起三个进程省事得多。

#### ⚠️ 运行期也会被冲掉（2026-10-19 实测，比休眠更隐蔽）

tmpfs 不只在「容器启动」时重建——平台 `workspace-init/init` 在**运行期也会反复跑**
（实测 10-19 当天 09:32 容器启动跑过一次、09:49 又跑过一次，伴随 editor-sdk /
sync-server 一起重启）。每次冲掉后：

| 现象 | 说明 |
|---|---|
| `cwb-*` 四个 program 从 supervisord **整体消失** | 连 uvicorn 都被 stop，不只是隧道 |
| 站点 502 | 代理机 18080 无监听，nginx 回源失败 |
| supervisord 主进程 uptime 正常 | **看不出重启过**，容易误判 |

对策是 `cwb-autorestore.sh` 看门狗（独立 setsid 进程，不挂 supervisord，平台
reload 动不了它），每 20s 巡检 `cwb.conf` 是否还在 include 目录、`cwb-tunnel`
是否还在 supervisord 里，缺任何一个就自动重跑 `install-autostart.sh`（幂等）：

```bash
setsid bash deploy/cwb-autorestore.sh >/tmp/cwb-autorestore.log 2>&1 &
```

> 局限说清楚：看门狗治的是「运行期被冲掉」；**沙箱休眠/容器重启照样会杀它**，
> 那种情况仍需手动跑一次 `install-autostart.sh`（见下文）。

### ⚠️ 沙箱休眠会杀掉长渲染任务（实测踩过）

沙箱**在闲置或资源紧张时会休眠重启**。实测 2026-10-15 晚上：复刻音色渲染跑了 70 分钟、
113 段时，沙箱静默重启，后果是：

| 现象 | 说明 |
|---|---|
| 后端/前端/隧道进程全没了 | `uptime` 只剩几分钟，PID 全变 |
| 站点 502 | 服务器 18080 无监听（SSH 反向隧道随沙箱一起断） |
| 渲染竹篮打水 | 已合成的 113 段 WAV 全作废，任务被标 failed |

**怎么判断沙箱是不是重启过**（别只看文件在不在）：

```bash
uptime                       # up 时间明显短于你的操作时长 = 重启过
ps -o lstart= -p 1           # PID 1 的启动时间
ps aux | grep uvicorn        # 后端还在不在
ss -tlnp | grep -E '5173|8000'   # 端口还有没有监听
```

> **教训**：判断「渲染是否还活着」**必须交叉验证进程**，不能只看 `clone_seg_*.wav`
> 数量在涨。文件增长可能是重启前遗留的滞后信息，而进程其实早没了。

恢复动作（一条命令）：

```bash
bash deploy/install-autostart.sh
```

跑完 backend / frontend / tunnel 三个进程都会回来，公网约 1 分钟后恢复。

**要跑长时间渲染，先确认沙箱不会中途休眠**；否则改成云端复刻（分钟级）更稳。

### 改完后端代码但不想打断正在跑的渲染

后端起服务时会把代码加载进内存，**改完代码必须重启才生效**。但复刻音色的
渲染可能跑 1.5 小时，直接重启会把任务打断。

`watch-and-restart.sh` 帮你挂着：盯着任务状态，一结束就自动重启，并验证新接口上线。

```bash
nohup bash deploy/watch-and-restart.sh <job_id> > /tmp/watch-restart.log 2>&1 &
tail -f /tmp/watch-restart.log     # 看它干活
```

重启走的是平台 supervisord（`$SV_DIR/bin/supervisord ctl restart cwb-backend`），
不是裸 `supervisorctl`（沙箱里没有这个命令）。

> **别在同库上起第二个实例做验证**：启动时的 `recover_orphans()` 会把所有
> `running` 任务标成失败，第二个进程会把主进程正在跑的任务误伤。
> 后端已加 `flock` 实例锁防这个（`/tmp/cwb-render.lock`），但要验证接口> 还是直接 `curl` 主实例更省事。

---


技术路线：**uv 管 Python 依赖** + **systemd 守护后端** + **nginx 托管前端并反代**。不使用 Docker。

---

## 固定出口代理（已废弃 · 2026-11-03）

> ⚠️ **此方案已废弃**（见 CHANGELOG 1.10.7）：微信出口改为**恒直连**，服务器出网 IP 已固定并加公众号后台「IP 白名单」，
> 后端不再读取 `WECHAT_PROXY`，无需 SSH 隧道 / SOCKS5 代理。以下内容仅作历史保留，勿再照做。

### （历史）固定出口代理（解决微信 IP 白名单漂移）

沙箱出网 IP 是**动态池**（一次采样能探到 20+ 个 IP，用 `deploy/probe-egress-ip.sh` 可复现），
而微信「API IP 白名单」**只收单个 IP、不收 CIDR**，逐个加白追不上。

解法：让**仅微信请求**走一台**固定公网 IP** 的机器出网。

```
后端(沙箱) --socks5://127.0.0.1:10800--> [ssh -D] --SSH--> 固定IP机器 --> api.weixin.qq.com
```

```bash
# 1) 起隧道守护（后台常驻，断线自愈）
setsid ./deploy/proxy-guard.sh > /tmp/proxy-guard.log 2>&1 &
# 自测：出口应为固定 IP
curl -s --socks5-hostname 127.0.0.1:10800 https://ifconfig.me/ip

# 2) 后端 .env 打开代理（只影响微信请求，不动 LLM 等）
#    WECHAT_PROXY=socks5://127.0.0.1:10800
#    WECHAT_PROXY_EXPECT_IP=49.233.223.252
```

然后把 `WECHAT_PROXY_EXPECT_IP` 这**一个 IP** 填进公众号后台白名单即可。
设置页「测试连通」返回的 `egress.ip` 就是当前实际出口。

- **已随 `install-autostart.sh` 托管**（`cwb-proxy` 服务，priority 219，早于后端启动）；
- 只需 `socksio`（`httpx[socks]`），已进 `requirements.txt`；
- 隧道偶发抖动时，微信客户端会对连接层错误自动重试 1 次，业务无感。

## 一、服务器准备

以 Ubuntu 22.04 / Debian 12 为例。

```bash
# 1. 系统依赖
sudo apt update
sudo apt install -y nginx git ffmpeg fonts-noto-cjk

# fonts-noto-cjk 必须装，否则后面 ffmpeg 烧中文字幕会出方块
# ffmpeg 二期做视频渲染才需要，一期可以先不装

# 2. Node（前端构建用，>= 20）
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
sudo apt install -y nodejs
sudo corepack enable

# 3. uv（Python 依赖与虚拟环境管理）
curl -LsSf https://astral.sh/uv/install.sh | sh
source ~/.bashrc
uv --version
```

### 配置国内源（强烈建议）

```bash
# uv 全局走清华源，写一次永久生效
mkdir -p ~/.config/uv
cat > ~/.config/uv/uv.toml <<'EOF'
[[index]]
url = "https://pypi.tuna.tsinghua.edu.cn/simple"
default = true
EOF

# npm 也换源（腾讯源国内最快）
npm config set registry https://mirrors.tencent.com/npm/
```

> 阿里云用户可换成 `https://mirrors.aliyun.com/pypi/simple/`。

---

## 二、部署代码

```bash
sudo mkdir -p /opt/content-workbench
sudo chown -R $USER:$USER /opt/content-workbench
cd /opt/content-workbench

# 用 git 拉，或直接 rsync/scp 上传整个项目
git clone <你的仓库地址> .
# 目录结构应为：backend/ frontend/ deploy/ docs/
```

---

## 三、后端

### 3.1 建虚拟环境装依赖

```bash
cd /opt/content-workbench/backend

# uv 自动建 .venv 并按 Python 版本要求挑解释器
uv venv --python 3.11

# 装依赖（走上面配好的清华源，通常 10 秒内完成）
uv pip install -r requirements.txt

# 验证
.venv/bin/python -c "import fastapi, sqlalchemy, openai; print('deps ok')"
```

> 想临时指定源：`uv pip install --index-url https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt`

### 3.2 配置环境变量

```bash
cd /opt/content-workbench/backend
cp .env.example .env
vim .env
```

**必须改的**：

```env
APP_ENV=prod
DEBUG=false
DEEPSEEK_API_KEY=sk-你的key
CORS_ORIGINS=https://你的域名.com          # 别留 localhost

# 用 Redis 的话（推荐，多进程共享缓存）
REDIS_URL=redis://127.0.0.1:6379/0
```

**要用公众号推送再填**：

```env
WECHAT_APPID=wx...
WECHAT_SECRET=...
```

### 3.3 数据目录

```bash
mkdir -p /opt/content-workbench/backend/data/{uploads,outputs}
```

SQLite 数据库文件会自动建在 `data/workbench.db`，目录不存在时代码里已做自动创建。

### 3.4 交给 systemd 守护

```bash
# 拷服务单元（路径按实际情况改 WorkingDirectory / ExecStart）
sudo cp /opt/content-workbench/deploy/systemd/cwb-backend.service /etc/systemd/system/
sudo vim /etc/systemd/system/cwb-backend.service   # 确认路径与用户

# 数据目录得让运行用户可写
sudo chown -R www-data:www-data /opt/content-workbench/backend/data

sudo systemctl daemon-reload
sudo systemctl enable --now cwb-backend

# 看状态与日志
sudo systemctl status cwb-backend
journalctl -u cwb-backend -f
```

**验证**：

```bash
curl -s http://127.0.0.1:8000/health
# {"status":"ok","env":"prod","llm_configured":true,"redis":true,"tools_enabled":true}
```

`llm_configured` 为 `false` → `.env` 里的 Key 没读到，检查文件位置与权限。

### 3.5 前端构建

```bash
cd /opt/content-workbench/frontend
pnpm install
pnpm build          # 产出 dist/

# 让 nginx 能读
sudo chown -R www-data:www-data dist
```

Node 环境没有 pnpm 时先 `corepack enable` 或 `npm i -g pnpm@10`。

> **备案号**：页面底部的 ICP 备案号不写在代码里，由构建期环境变量注入。
> 在 `frontend/.env`（不进 git）里加一行再构建，否则页脚不渲染：
>
> ```bash
> echo 'VITE_ICP_BEIAN=你的备案号' >> /opt/content-workbench/frontend/.env
> ```
>
> 只在中国大陆境内公网部署时需要。忘了配的话，`pnpm build` 会打印 `[cwb] 未设置 VITE_ICP_BEIAN` 警告。

> **pnpm 版本注意**：项目锁定了 pnpm 10。pnpm 12 默认有个 `minimumReleaseAge`（24 小时）策略，会把刚发布的依赖判为「太新」而拒绝安装。`frontend/.npmrc` 里已写 `minimumReleaseAge=0` 兜底。

### 3.6 配置 nginx

```bash
sudo cp /opt/content-workbench/deploy/nginx.conf /etc/nginx/conf.d/content-workbench.conf
sudo vim /etc/nginx/conf.d/content-workbench.conf   # 改 root 路径和 server_name

sudo nginx -t          # 语法检查
sudo systemctl reload nginx
```

打开 `http://你的域名` 应能看到工作台。

---

## 四、上线检查清单

- [ ] `uv pip install -r requirements.txt` 成功，`.venv/bin/python -c "import fastapi"` 通过
- [ ] `.env` 里 `DEEPSEEK_API_KEY` 已填，`/health` 返回 `llm_configured: true`
- [ ] `APP_ENV=prod`、`DEBUG=false`
- [ ] `CORS_ORIGINS` 改成真实域名（别留 localhost）
- [ ] `systemctl status cwb-backend` 是 active (running)
- [ ] `pnpm build` 产出 `dist/index.html`
- [ ] 境内公网部署时 `frontend/.env` 已配 `VITE_ICP_BEIAN`，页面底部能看到备案号
- [ ] `nginx -t` 通过，页面能打开
- [ ] **nginx 里 `proxy_buffering off` 已生效** —— 打开对话页发一句，字应是逐字冒出来的，不是等半天一次性刷出
- [ ] `data/` 目录权限正确（www-data 可写），重启服务后数据仍在
- [ ] 用到公众号推送的话，服务器公网 IP 已加进微信「API IP 白名单」
- [ ] **没有**为了图省事把 `TOOL_REQUIRE_CONFIRM` 清空

---

## 五、日常运维

```bash
# 更新代码
cd /opt/content-workbench && git pull
cd backend && uv pip install -r requirements.txt    # 依赖有变才需要
sudo systemctl restart cwb-backend

# 前端有改动
cd /opt/content-workbench/frontend && pnpm install && pnpm build

# 看日志
journalctl -u cwb-backend -n 200 --no-pager
journalctl -u cwb-backend -f

# 备份数据库（SQLite 直接拷文件，建议先停服务或用 .backup 命令）
cd /opt/content-workbench/backend
sqlite3 data/workbench.db ".backup '/backup/workbench-$(date +%F).db'"
```

---

## 六、切换到 PostgreSQL（数据量上来后）

```bash
uv pip install asyncpg
```

`.env` 改一行，代码零改动：

```env
DATABASE_URL=postgresql+asyncpg://user:pass@localhost:5432/workbench
```

建表由启动时的 `init_db()` 自动完成。生产环境建议后续引入 Alembic 做版本化迁移（现在的 `create_all` 只建表、不改已存在的表结构）。

---

## 七、Redis（可选但推荐）

未配 `REDIS_URL` 时自动降级为进程内内存缓存。**用 `--workers 2` 起多进程时建议配 Redis**，否则多个 worker 各存一份缓存，渲染任务可能查不到。

```bash
sudo apt install -y redis-server
sudo systemctl enable --now redis-server
```

`.env` 里 `REDIS_URL=redis://127.0.0.1:6379/0`，重启后端即可。

---

## 八、视频渲染模块（已上线，真实渲染管线）

视频生产模块已实现为真实渲染管线（不再是占位）：文案 → edge-tts 合成语音（同步收集 SentenceBoundary 句级时间轴，**不再需要 faster-whisper 转写，也不需要 moviepy / libreoffice**）→ PIL 生成渐变字幕卡片 → ffmpeg `zoompan` 推拉运镜（Ken Burns）逐场景渲染 → concat 合成 MP4（720x1280 竖屏，libopenh264，10s 视频约 5s 完成）。

```bash
# 系统依赖：ffmpeg + 中文字体
sudo apt install -y ffmpeg fonts-noto-cjk

# 可选：PPT 每页真实画面渲染（没有的话 PPT 视频退回文字卡片）
sudo apt install -y --no-install-recommends libreoffice-impress poppler-utils

cd /opt/content-workbench/backend
# requirements.txt 已包含 edge-tts / pillow / python-pptx，直接安装即可
pip install -r requirements.txt

sudo systemctl restart cwb-backend
```

要点：

- **任务持久化**：渲染任务存 `render_jobs` 表，服务重启后 `recover_orphans()` 会把中断的任务标记为失败（前端可点「重试」重新渲染），进度实时写库。
- **串行渲染**：`asyncio.Semaphore(1)` 保证同一时刻只有一个渲染任务，避免 CPU 打满影响 API。**架构建议**：渲染是 CPU/GPU 密集任务，后续若并发需求上来，把 `_run()` 改成投递到 Celery/RQ 队列、单独起 worker（可再写一个 systemd unit）。渲染状态机与接口层已分离，改造只需动 `services/render.py`。
