# =============================================================================
# 内容工作台 · 常用运维快捷命令
#
#   make help            看全部命令（默认目标）
#   make init            第一次用：生成本地部署配置模板
#   make deploy          一键：构建 → 备份 → 同步 → 重启
#
# 【私有信息不入库】服务器 IP、登录用户、密钥路径属于部署方私有信息，
# 不写进本文件（本文件会提交进公开仓库）。真实值放本地 .env.deploy，
# 已被 .gitignore 忽略。也可以临时覆盖：
#     make status DEPLOY_HOST=cwb@1.2.3.4
#     make restart SVC=miniapp-backend
#
# 【远程 shell 是 fish】复杂脚本一律通过 `... | ssh ... 'sudo bash -s'`
# 交给 bash 执行，避免 fish 不支持 bash 语法（历史上踩过 for 循环的坑）。
# 单条 systemctl/curl 这类外部命令不受影响。
#
# 【路径约定】miniapp-* 目标会用到仓库外的兄弟目录（deploy/ 与
# miniapp-backend/），可用 DEPLOY_SCRIPT / MINIAPP_SRC 变量覆盖。
# 目录不存在时会直接给出提示，不会静默失败。
# =============================================================================

.DEFAULT_GOAL := help
.NOTPARALLEL:

# ------------------------------------------------------------------ 配置
# 本地私有配置优先：.env.deploy 里定义的变量会盖住下面的 ?= 默认值。
# 注意：不要把行内注释写在赋值后面，GNU make 会把 # 之前的空格算进变量值。
-include .env.deploy

# 必填，形如 cwb@1.2.3.4。留空时所有联网命令都会被 _check 拦下
DEPLOY_HOST ?=
# 默认 SSH 密钥；deploy.sh 也读同名的 SSH_KEY 环境变量
SSH_KEY ?= $(HOME)/.ssh/cwb_deploy_key
SSH_OPTS ?= -o StrictHostKeyChecking=no -o ConnectTimeout=10
REMOTE_ROOT ?= /opt/content-workbench
SVC ?= cwb-backend
MINI_SVC ?= miniapp-backend
PKG ?= pnpm
PY ?= python3
# make logs 的默认行数
N ?= 50
# 小程序 Go 后端：源码、部署脚本、二进制都在仓库外的兄弟目录
MINIAPP_SRC ?= $(CURDIR)/../miniapp-backend
DEPLOY_SCRIPT ?= $(CURDIR)/../deploy/deploy.sh
# 简单展开一次即可，避免每次引用都起一个 shell
DEPLOY_IP := $(shell echo "$(DEPLOY_HOST)" | sed 's/.*@//')

SSH := ssh -i $(SSH_KEY) $(SSH_OPTS) $(strip $(DEPLOY_HOST))
# 上传刻意不用 scp：与目标机短时两次建连（scp + ssh）容易在 KEX 阶段被重置
# （实测 kex_exchange_identification: Connection reset by peer，ssh 却正常）。
# sync-* 统一走「tar 走 stdin + 远端 sh -c 解包」的单连接方案，少一次握手。

.PHONY: help init config _check \
        status health logs \
        start stop restart start-all stop-all restart-all \
        ssh build type-check test \
        backup backups sync-backend sync-frontend sync deploy \
        miniapp-build miniapp-deploy

# 所有联网命令都过这一关：没配主机就给可执行的提示，而不是甩一串 ssh 报错
_check:
	@test -n "$(strip $(DEPLOY_HOST))" || { \
	  echo "✗ 未配置部署主机。"; \
	  echo "  第一次用：make init  → 编辑 .env.deploy 填 DEPLOY_HOST"; \
	  echo "  或临时指定：make status DEPLOY_HOST=cwb@服务器IP"; \
	  exit 1; \
	}
	@case "$(strip $(DEPLOY_HOST))" in \
	  *your-server-ip*|*x.x.x.x*|*example.com*) \
	    echo "✗ DEPLOY_HOST 还是占位值：$(DEPLOY_HOST)"; \
	    echo "  请编辑 .env.deploy 填真实主机"; exit 1;; \
	esac

# ------------------------------------------------------------------ 帮助
help: ## 显示这份帮助
	@printf '\n内容工作台 · 常用命令\n\n'
	@printf '  配置\n'
	@printf '    %-30s %s\n' 'make init' '生成 .env.deploy 模板（第一次用）'
	@printf '    %-30s %s\n' 'make config' '查看当前生效配置（不联网）'
	@printf '\n  服务器（远程）\n'
	@printf '    %-30s %s\n' 'make status' '查看两个服务运行状态'
	@printf '    %-30s %s\n' 'make health' '探活 8001 / 8090 两个健康接口'
	@printf '    %-30s %s\n' 'make logs' '看日志（SVC=... N=100 可覆盖）'
	@printf '    %-30s %s\n' 'make start / stop / restart' '启停单个服务（SVC 可切）'
	@printf '    %-30s %s\n' 'make start-all / stop-all' '启停两个服务'
	@printf '    %-30s %s\n' 'make restart-all' '重启两个服务'
	@printf '    %-30s %s\n' 'make ssh' '登录服务器'
	@printf '\n  本地开发\n'
	@printf '    %-30s %s\n' 'make build' '前端生产构建'
	@printf '    %-30s %s\n' 'make type-check' '前端类型检查'
	@printf '    %-30s %s\n' 'make test' '后端单测'
	@printf '\n  上传代码 / 部署\n'
	@printf '    %-30s %s\n' 'make sync-backend' '只传后端代码（不动服务器 .env）'
	@printf '    %-30s %s\n' 'make sync-frontend' '构建并传前端 dist'
	@printf '    %-30s %s\n' 'make sync' '两者都传'
	@printf '    %-30s %s\n' 'make backup' '远端备份 app + dist'
	@printf '    %-30s %s\n' 'make backups' '列出远端备份'
	@printf '    %-30s %s\n' 'make deploy' '一键：构建 → 备份 → 同步 → 重启'
	@printf '\n  小程序 Go 后端（依赖仓库外的 deploy/ 与 miniapp-backend/）\n'
	@printf '    %-30s %s\n' 'make miniapp-build' '交叉编译 linux/amd64 二进制'
	@printf '    %-30s %s\n' 'make miniapp-deploy' '升级线上小程序后端'
	@printf '\n  变量示例\n'
	@printf '    %-38s %s\n' 'make restart SVC=miniapp-backend' '只重启小程序后端'
	@printf '    %-38s %s\n' 'make logs N=200' '看最近 200 行日志'
	@printf '    %-38s %s\n' 'make deploy DEPLOY_HOST=cwb@x.x.x.x' '临时换目标机'
	@printf '\n  提示：sync-backend 是增量覆盖，不会删除服务器上多余的旧文件。\n\n'

init: ## 生成本地部署配置模板
	@if [ -f .env.deploy ]; then \
	  echo ".env.deploy 已存在，未改动"; \
	else \
	  printf '%s\n' \
	    '# 本地部署配置 —— 本文件被 .gitignore 忽略，不要提交' \
	    '# 服务器 IP / 登录用户属于部署方私有信息，不进公开仓库' \
	    '# 改成你的真实主机，例如：DEPLOY_HOST = cwb@192.0.2.10' \
	    'DEPLOY_HOST = ' \
	    '# SSH_KEY = /path/to/your_key     # 默认 ~/.ssh/cwb_deploy_key' \
	    '# REMOTE_ROOT = /opt/content-workbench' \
	    > .env.deploy; \
	  echo "✓ 已生成 .env.deploy，请填 DEPLOY_HOST 后重试"; \
	fi
	@chmod 600 .env.deploy 2>/dev/null || true
	@echo "  当前 DEPLOY_HOST = $(if $(strip $(DEPLOY_HOST)),$(DEPLOY_HOST),<未配置>)"

config: ## 查看当前生效配置
	@printf '%s\n' \
	  "  DEPLOY_HOST   = $(if $(strip $(DEPLOY_HOST)),$(DEPLOY_HOST),<未配置>)" \
	  "  SSH_KEY       = $(SSH_KEY)" \
	  "  REMOTE_ROOT   = $(REMOTE_ROOT)" \
	  "  SVC / MINI    = $(SVC) / $(MINI_SVC)" \
	  "  PKG / PY      = $(PKG) / $(PY)" \
	  "  DEPLOY_SCRIPT = $(DEPLOY_SCRIPT)" \
	  "  MINIAPP_SRC   = $(MINIAPP_SRC)"

# ------------------------------------------------------------------ 服务器状态
status: _check ## 查看两个服务运行状态
	@$(SSH) 'printf "  %-20s " "$(SVC):"; sudo systemctl is-active $(SVC); printf "  %-20s " "$(MINI_SVC):"; sudo systemctl is-active $(MINI_SVC)'

health: _check ## 探活两个健康接口
	@$(SSH) 'printf "  [8001] "; curl -s --max-time 5 http://127.0.0.1:8001/api/health; echo; printf "  [8090] "; curl -s --max-time 5 http://127.0.0.1:8090/api/health; echo'

logs: _check ## 看日志（SVC=... N=...）
	@$(SSH) 'sudo journalctl -u $(SVC) -n $(N) --no-pager'

# ------------------------------------------------------------------ 服务控制
start: _check ## 启动服务（SVC 可切）
	@$(SSH) 'sudo systemctl start $(SVC)' && echo "✓ 已启动 $(SVC)"

stop: _check ## 停止服务（SVC 可切）
	@$(SSH) 'sudo systemctl stop $(SVC)' && echo "✓ 已停止 $(SVC)"

restart: _check ## 重启服务（SVC 可切）
	@$(SSH) 'sudo systemctl restart $(SVC)' && echo "✓ 已重启 $(SVC)"

start-all: _check ## 启动两个服务
	@$(SSH) 'sudo systemctl start $(SVC) $(MINI_SVC)' && echo "✓ 已启动两个服务"

stop-all: _check ## 停止两个服务
	@$(SSH) 'sudo systemctl stop $(SVC) $(MINI_SVC)' && echo "✓ 已停止两个服务"

restart-all: _check ## 重启两个服务
	@$(SSH) 'sudo systemctl restart $(SVC) $(MINI_SVC)' && echo "✓ 已重启两个服务"

ssh: _check ## 登录服务器
	@echo "登录 $(DEPLOY_HOST) …（exit 退出）"
	@$(SSH)

# ------------------------------------------------------------------ 本地构建
build: ## 前端生产构建
	@echo "→ 前端构建 …"
	@cd $(CURDIR)/frontend && $(PKG) run build

type-check: ## 前端类型检查
	@cd $(CURDIR)/frontend && $(PKG) run type-check

test: ## 后端单测
	@cd $(CURDIR)/backend && $(PY) -m pytest -q

# ------------------------------------------------------------------ 远端备份
backup: _check ## 备份远端 app + dist
	@printf '%s\n' \
	  'set -e' \
	  'S=$(REMOTE_ROOT)/backup-$$(date +%Y%m%d-%H%M%S)' \
	  'mkdir -p $$S' \
	  'cp -a $(REMOTE_ROOT)/backend/app $$S/app' \
	  'cp -a $(REMOTE_ROOT)/frontend/dist $$S/dist' \
	  'echo "✓ 备份完成：$$S"' \
	  | $(SSH) 'sudo bash -s'

backups: _check ## 列出远端备份
	@$(SSH) 'ls -1dt $(REMOTE_ROOT)/backup-* 2>/dev/null | head -20 || echo "暂无备份"'

# ------------------------------------------------------------------ 上传代码
# 只传 backend/app，绝不覆盖服务器上的 .env（里面有 JWT_SECRET / 各类 API Key）
sync-backend: _check ## 只传后端代码
	@echo "→ 本地语法预检 …"
	@$(PY) -m compileall -q $(CURDIR)/backend/app
	@echo "→ 打包并同步后端代码（单连接）…"
	@tar czf - -C $(CURDIR) \
	    --exclude='__pycache__' --exclude='*.pyc' --exclude='.pytest_cache' \
	    backend/app \
	  | $(SSH) 'sudo sh -c "set -e; rm -rf /tmp/cwb-backend-new; mkdir -p /tmp/cwb-backend-new; tar xzf - -C /tmp/cwb-backend-new; cp -r /tmp/cwb-backend-new/backend/app/. $(REMOTE_ROOT)/backend/app/; echo ✓ 后端代码已同步（.env 未改动，需 make restart 才生效）"'

sync-frontend: _check build ## 构建并传前端 dist
	@echo "→ 打包并同步前端产物（单连接）…"
	@tar czf - -C $(CURDIR)/frontend/dist . \
	  | $(SSH) 'sudo sh -c "set -e; rm -rf /tmp/cwb-dist-new; mkdir -p /tmp/cwb-dist-new; tar xzf - -C /tmp/cwb-dist-new; cp -r /tmp/cwb-dist-new/. $(REMOTE_ROOT)/frontend/dist/; echo ✓ 前端产物已同步（静态文件无需重启服务）"'

sync: sync-backend sync-frontend ## 前后端一起传

deploy: _check build backup sync restart ## 一键：构建 → 备份 → 同步 → 重启
	@echo "✓ 部署完成，跑 make health 确认"

# ------------------------------------------------------------------ 小程序 Go 后端
miniapp-build: ## 交叉编译小程序后端（linux/amd64 静态二进制）
	@test -d "$(MINIAPP_SRC)" || { echo "✗ 找不到 Go 源码目录 $(MINIAPP_SRC)"; echo "  可用 MINIAPP_SRC=/path 覆盖"; exit 1; }
	@command -v go >/dev/null || { echo "✗ 本机没装 go"; exit 1; }
	@test -f "$(DEPLOY_SCRIPT)" || { echo "✗ 找不到 $(DEPLOY_SCRIPT)"; exit 1; }
	@echo "→ 交叉编译 Go 后端 …"
	@cd "$(MINIAPP_SRC)" && GOOS=linux GOARCH=amd64 CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o "$(dir $(DEPLOY_SCRIPT))miniapp-backend" ./cmd/server
	@echo "✓ 已生成 $(dir $(DEPLOY_SCRIPT))miniapp-backend"

# deploy.sh 固定以 cwb 用户登录且只吃 IP（DEPLOY_HOST 的用户名部分对它无效），
# 并且要求「当前目录」下就有 miniapp-backend 二进制，所以必须先 cd 过去。
miniapp-deploy: _check ## 升级线上小程序 Go 后端
	@test -f "$(DEPLOY_SCRIPT)" || { echo "✗ 找不到 $(DEPLOY_SCRIPT)"; echo "  该脚本在工作区 deploy/ 目录，不在本仓库内"; exit 1; }
	@test -f "$(dir $(DEPLOY_SCRIPT))miniapp-backend" || { echo "✗ 缺 miniapp-backend 二进制，先跑 make miniapp-build"; exit 1; }
	@cd "$(dir $(DEPLOY_SCRIPT))" && SSH_KEY="$(SSH_KEY)" bash ./deploy.sh "$(DEPLOY_IP)" --upgrade
