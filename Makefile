# ============================================================
# 悦溪平台 Makefile
#
# 模式约定:
#   1) macOS/Linux 本机开发（中间件/RAG 走 Docker，前端本机运行，后端断点调试走 VSCode Debug）
#      make dev-deps-up
#      make frontends-install
#      make dev-frontend-shell
#   2) 本地 Docker 部署（整套服务容器运行，加载 docker-compose.override.yml）
#      make build
#      make up
#      make down
#   3) 宿主机单服务调试（大部分服务走 Docker，目标后端/RAG 服务在宿主机运行）
#      make docker-local-build
#      make docker-local-up
#      make docker-local-down
#   4) GPU/服务器 Docker（Windows/Linux/服务器）
#      make docker-gpu-build
#      make docker-gpu-up
#      make docker-gpu-down
#   单服务操作:
#      make docker-local-restart-service SERVICE=gateway
#      make docker-local-rebuild-service SERVICE=core-business-frontend
#      make docker-gpu-restart-service SERVICE=gateway
# ============================================================

.DEFAULT_GOAL := help
SHELL := /bin/sh

# ------------------------------------------------------------
# 基础变量
# ------------------------------------------------------------
ENV_FILE ?= deploy/.env
COMPOSE ?= docker compose
PNPM ?= pnpm
PYTHON ?= python3
SERVICE ?=
N ?= 1
PYTHON_BASE_TAG ?= jonex/python-base:local
# 构建源镜像开关：false=国内源（腾讯云 pip/apt、npmmirror、清华 uv），true=国外官方源
# （pypi.org / deb.debian.org / registry.npmjs.org）。仅影响构建期下载，不影响运行。
USE_OVERSEAS_MIRROR ?= false
PERF_TAIL ?= 1000
METERING_RETENTION_DAYS ?= 90

-include $(ENV_FILE)
export

DB_HOST ?= 127.0.0.1
DB_PORT ?= 5432
DB_USERNAME ?= jonex
DB_PASSWORD ?= jonex123
DB_NAME ?= jonex

UNAME_S := $(shell uname -s)

# Compose 文件组合规则:
# - docker-compose.yml: 通用基线（无 GPU 依赖，所有平台可用）
# - docker-compose.gpu.yml: GPU 覆盖（为 atomic-rag 启用 NVIDIA GPU 加速）
# - docker-compose.mac.yml: Mac CPU 覆盖（降低 atomic-rag 资源占用）
# - docker-compose.override.yml: 通用本地覆盖（端口暴露/常规调试）
# - docker-compose.debug.yml: 单服务宿主机调试覆盖（sidecar 指向宿主机后端/atomic-rag）
COMPOSE_FILES := -f docker-compose.yml
COMPOSE_GPU_FILES := $(COMPOSE_FILES) -f docker-compose.gpu.yml
COMPOSE_MAC_FILES := $(COMPOSE_FILES) -f docker-compose.mac.yml
COMPOSE_LOCAL_FILES := $(COMPOSE_FILES)
ifeq ($(UNAME_S),Darwin)
COMPOSE_LOCAL_FILES := $(COMPOSE_MAC_FILES)
endif
COMPOSE_OVERRIDE_FILES := $(COMPOSE_LOCAL_FILES) -f docker-compose.override.yml
COMPOSE_DEV_FILES := $(COMPOSE_LOCAL_FILES) -f docker-compose.debug.yml

DOCKER_GPU := cd deploy && $(COMPOSE) $(COMPOSE_GPU_FILES)
DOCKER_BASE := cd deploy && $(COMPOSE) $(COMPOSE_LOCAL_FILES)
DOCKER_OVERRIDE := cd deploy && $(COMPOSE) $(COMPOSE_OVERRIDE_FILES)
DOCKER_DEV := cd deploy && $(COMPOSE) $(COMPOSE_DEV_FILES)

MIDDLEWARE_SERVICES := postgres redis etcd minio milvus
RAG_SERVICES := lightrag atomic-rag
BACKEND_SERVICES := gateway sidecar knowledge-base-service business-domain-service platform-service mcp-server
FRONTEND_SERVICES := frontend-gateway shell-frontend core-business-frontend platform-management-frontend ecosystem-management-frontend

FRONTENDS_DIR := frontends
SHELL_APP := @jonex/shell
CORE_APP := @jonex/core-business
PLATFORM_APP := @jonex/platform-management
ECOSYSTEM_APP := @jonex/ecosystem-management
MAIN_FRONTEND_FILTERS := --filter $(SHELL_APP) --filter $(CORE_APP) --filter $(PLATFORM_APP) --filter $(ECOSYSTEM_APP)

LOCAL_BACKEND_ENV := ENV=dev DB_HOST=127.0.0.1 DB_PORT=$(DB_PORT) DB_USERNAME=$(DB_USERNAME) DB_PASSWORD=$(DB_PASSWORD) DB_NAME=$(DB_NAME) REDIS_URL=redis://127.0.0.1:6379/0 MILVUS_HOST=127.0.0.1 MILVUS_PORT=19530 SIDECAR_URL=http://127.0.0.1:8001 KNOWLEDGE_BASE_URL=http://127.0.0.1:8003 BUSINESS_DOMAIN_URL=http://127.0.0.1:8005 ATOMIC_RAG_URL=http://127.0.0.1:8004 PLATFORM_URL=http://127.0.0.1:8006

.PHONY: help init version \
	build-python-base build build-local build-overseas build-gpu build-prod build-server build-infra build-rag build-backend build-frontend build-service build-sidecar build-knowledge-base build-business-domain build-platform \
	up up-local up-detached up-gpu up-server up-prod up-infra up-rag up-backend up-frontend up-service up-sidecar up-knowledge-base up-business-domain up-platform \
	down down-local down-gpu down-server down-prod down-v stop stop-service down-service down-gateway down-sidecar down-frontend down-knowledge-base down-business-domain down-platform \
	restart restart-gpu restart-server restart-prod restart-service recreate-service rebuild-service \
	ps ps-gpu ps-server ps-prod \
	logs logs-gpu logs-server logs-prod logs-service logs-gateway logs-sidecar logs-knowledge-base logs-business-domain logs-platform logs-lightrag logs-postgres logs-redis logs-milvus logs-etcd logs-minio logs-infra logs-rag logs-backend logs-frontend \
	perf perf-ingest perf-reconcile perf-chunk perf-thinking perf-extract perf-audit perf-search perf-llm perf-llm-detail metering-rollup \
	dev dev-deps-up dev-infra-up dev-rag-up dev-deps-down dev-deps-logs \
	docker-local-build docker-local-up docker-local-down docker-local-ps docker-local-logs docker-local-restart docker-local-up-service docker-local-down-service docker-local-logs-service docker-local-restart-service docker-local-recreate-service docker-local-rebuild-service \
	docker-gpu-build docker-gpu-up docker-gpu-down docker-gpu-ps docker-gpu-logs docker-gpu-restart docker-gpu-up-service docker-gpu-down-service docker-gpu-logs-service docker-gpu-restart-service docker-gpu-rebuild-service \
	frontends-install frontends-env dev-gateway dev-frontend dev-frontend-all dev-frontend-shell dev-frontend-core dev-frontend-platform dev-frontend-ecosystem \
	preview-core preview-platform preview-ecosystem preview-all \
	rebuild-gateway rebuild-sidecar rebuild-knowledge-base rebuild-business-domain rebuild-platform \
	rebuild-frontend-gateway rebuild-shell-frontend rebuild-core-business-frontend rebuild-platform-management-frontend rebuild-ecosystem-management-frontend \
	restart-gateway restart-sidecar restart-knowledge-base restart-business-domain restart-platform restart-frontend restart-frontend-gateway restart-shell-frontend restart-core-business-frontend restart-platform-management-frontend restart-ecosystem-management-frontend \
	scale-knowledge-base up-postgres up-redis up-milvus up-lightrag pull-lightrag init-db db-migrate db-migrate-after-up test clean \
	exec-postgres exec-gateway exec-sidecar exec-knowledge-base exec-business-domain exec-platform exec-lightrag exec-shell-frontend \
	shell-postgres shell-gateway shell-sidecar shell-knowledge-base shell-business-domain shell-platform shell-lightrag shell-frontend \
	_require-service

# ------------------------------------------------------------
# 帮助信息
# ------------------------------------------------------------
help: ## 显示帮助信息
	@echo "=== 悦溪平台 Makefile ==="
	@echo ""
	@echo "初始化:"
	@echo "  make init                         初始化 deploy/.env、deploy/.env.rag、deploy/.env.mcp、前端 .env"
	@echo "  make frontends-env                仅初始化各前端子应用的 .env（从 .env.example 复制）"
	@echo "  make version                      查看 Docker / Compose 版本"
	@echo ""
	@echo "模式一: macOS/Linux 本机开发"
	@echo "  make dev-deps-up                  启动 postgres/redis/milvus/RAG 等本地依赖"
	@echo "  make dev-deps-down                停止本地依赖（保留容器和数据卷）"
	@echo "  make dev-deps-logs                查看本地依赖日志"
	@echo "  make frontends-install            安装/同步 frontends workspace 依赖"
	@echo "  make dev-gateway                   启动 Dev Gateway 统一入口: http://localhost:8080"
	@echo "  make dev-frontend                 一键启动 shell + 三个 TS 子应用（不含 Dev Gateway）"
	@echo "  make dev-frontend-all             一键启动 Dev Gateway + 全部 4 个前端子应用"
	@echo "  make dev-frontend-shell           启动 shell: http://localhost:5173"
	@echo "  make dev-frontend-core            启动业务领域管理: http://localhost:5175"
	@echo "  make dev-frontend-ecosystem       启动生态管理: http://localhost:5176"
	@echo "  make dev-frontend-platform        启动平台管理: http://localhost:5177"
	@echo "  后端本机断点调试请使用 VSCode Debug，见 dev-guide.macos.md"
	@echo ""
	@echo "模式二: 本地 Docker 部署（override）"
	@echo "  make build                        构建本地 Docker 镜像（Mac 自动叠加 docker-compose.mac.yml）"
	@echo "  make build-overseas               构建本地 Docker 镜像（国外官方源）"
	@echo "  make up                           启动本地 Docker 部署（加载 docker-compose.override.yml）"
	@echo "  make ps                           查看本地 Docker 状态"
	@echo "  make logs                         查看本地 Docker 日志"
	@echo "  make down                         停止并删除本地 Docker 部署"
	@echo "  make restart                      重启本地 Docker 部署"
	@echo ""
	@echo "模式三: 宿主机单服务调试（debug compose）"
	@echo "  make docker-local-build           构建宿主机单服务调试镜像"
	@echo "  make docker-local-up              启动宿主机单服务调试（加载 docker-compose.debug.yml）"
	@echo "  make docker-local-down            停止并删除宿主机单服务调试环境"
	@echo "  make docker-local-ps              查看宿主机单服务调试状态"
	@echo "  make docker-local-logs            查看宿主机单服务调试日志"
	@echo "  make docker-local-restart         重启宿主机单服务调试环境"
	@echo ""
	@echo "模式四: 生产/服务器 Docker（无 GPU，仅 docker-compose.yml）"
	@echo "  make up-prod                       启动生产部署"
	@echo "  make down-prod                     停止并删除生产部署"
	@echo "  make build-prod                    构建生产镜像"
	@echo "  make ps-prod                       查看生产服务状态"
	@echo "  make logs-prod                     查看生产全部日志"
	@echo "  make restart-prod                  重启生产部署"
	@echo ""
	@echo "模式五: GPU/服务器 Docker（需 NVIDIA Container Toolkit）"
	@echo "  make docker-gpu-build             构建 GPU/服务器镜像（叠加 docker-compose.gpu.yml）"
	@echo "  make docker-gpu-up                启动 GPU/服务器部署"
	@echo "  make docker-gpu-down              停止并删除 GPU/服务器部署"
	@echo "  make docker-gpu-ps                查看 GPU/服务器部署状态"
	@echo "  make docker-gpu-logs              查看 GPU/服务器部署日志"
	@echo "  make docker-gpu-restart           重启 GPU/服务器部署"
	@echo ""
	@echo "单服务操作:"
	@echo "  make docker-local-restart-service SERVICE=gateway"
	@echo "  make docker-local-rebuild-service SERVICE=core-business-frontend"
	@echo "  make logs-service SERVICE=platform-service"
	@echo "  make docker-local-logs-service SERVICE=knowledge-base-service"
	@echo "  make docker-local-down-service SERVICE=shell-frontend"
	@echo "  make docker-gpu-restart-service SERVICE=gateway"
	@echo "  make docker-gpu-logs-service SERVICE=shell-frontend"
	@echo ""
	@echo "性能耗时日志（摄入链路埋点，详见 docs/ingestion-timing-metrics-design.md）:"
	@echo "  make perf                         汇总查看 ingest_timing + reconcile_timing"
	@echo "  make perf-ingest                  worker 分阶段耗时 ingest_timing（atomic-rag）"
	@echo "  make perf-reconcile               对账入图库耗时 reconcile_timing（knowledge-base-service）"
	@echo "  make perf-chunk                   LightRAG 内部 chunk_timing（lightrag，拆 extract/merge/persist）"
	@echo "  make perf-thinking                关思考注入 thinking.disabled（llm-gateway）"
	@echo "  make perf-extract                 抽取场景调用 lightrag_extract（llm-gateway，看 latency/token）"
	@echo "  make perf-search                  本体检索 RAG 线路耗时 ontology_search_timing（knowledge-base-service，多库检索/融合）"
	@echo "  make perf-audit                   审计表耗时 audit_logs.duration_ms（postgres）"
	@echo "  make perf-llm                     LLM 计量可读汇总（postgres 视图，DIM=doc|trace|daily）"
	@echo "  make perf-llm-detail              LLM 计量明细可读视图（关联 kb/doc 名称 + 本地时区）"
	@echo "  make metering-rollup              汇总 llm_usage_daily +（dry-run）统计可清理明细；CONFIRM=1 才删除超 $(METERING_RETENTION_DAYS) 天明细"
	@echo "  # 默认查最近 $(PERF_TAIL) 行历史，可调: make perf-ingest PERF_TAIL=5000"
	@echo ""
	@echo "Compose 文件规则:"
	@echo "  docker-compose.yml                通用基线（无 GPU 依赖，所有平台可用）"
	@echo "  docker-compose.gpu.yml            GPU 覆盖（启用 NVIDIA GPU 加速 atomic-rag）"
	@echo "  docker-compose.mac.yml            Mac CPU 覆盖（降低 atomic-rag 资源占用）"
	@echo "  docker-compose.override.yml       通用本地覆盖（端口暴露/常规调试）"
	@echo "  docker-compose.debug.yml          单服务宿主机调试覆盖（sidecar 指向宿主机后端/atomic-rag）"
	@echo ""
	@echo "常用服务名:"
	@echo "  后端: gateway sidecar knowledge-base-service business-domain-service platform-service mcp-server"
	@echo "  前端: shell-frontend core-business-frontend platform-management-frontend ecosystem-management-frontend"
	@echo "  RAG/原子能力: lightrag atomic-rag"
	@echo "  中间件: postgres redis etcd minio milvus"

# ------------------------------------------------------------
# 初始化
# ------------------------------------------------------------
init: ## 初始化环境配置
	@echo "=== 初始化环境 ==="
	@if [ ! -f deploy/.env ]; then \
		cp deploy/.env.example deploy/.env; \
		echo "已创建平台配置: deploy/.env"; \
	else \
		echo "平台配置已存在: deploy/.env"; \
	fi
	@if [ ! -f deploy/.env.rag ]; then \
		cp deploy/.env.rag.example deploy/.env.rag; \
		echo "已创建 RAG 配置: deploy/.env.rag"; \
	else \
		echo "RAG 配置已存在: deploy/.env.rag"; \
	fi
	@if [ ! -f deploy/.env.mcp ]; then \
		cp deploy/.env.mcp.example deploy/.env.mcp; \
		echo "已创建 MCP 配置: deploy/.env.mcp"; \
	else \
		echo "MCP 配置已存在: deploy/.env.mcp"; \
	fi
	@if [ ! -f deploy/.env.openkb ]; then \
		cp deploy/.env.openkb.example deploy/.env.openkb; \
		echo "已创建 OpenKB 配置: deploy/.env.openkb"; \
	else \
		echo "OpenKB 配置已存在: deploy/.env.openkb"; \
	fi
	@echo "下一步: 按需修改 deploy/.env、deploy/.env.rag、deploy/.env.mcp 和 deploy/.env.openkb"
	@$(MAKE) frontends-env

frontends-env: ## 初始化各前端子应用 .env
	@echo "=== 初始化前端环境变量 ==="
	@for app in shell core-business ecosystem-management platform-management dev-gateway; do \
		if [ ! -f frontends/$$app/.env.example ]; then \
			echo "模板缺失: frontends/$$app/.env.example"; \
		elif [ ! -f frontends/$$app/.env ]; then \
			cp frontends/$$app/.env.example frontends/$$app/.env; \
			echo "已创建: frontends/$$app/.env"; \
		else \
			echo "已存在: frontends/$$app/.env"; \
		fi; \
	done

version: ## 显示 Docker/Compose 版本
	@docker --version
	@$(COMPOSE) version

# ------------------------------------------------------------
# Docker 构建（共享 Python_Base + COMPOSE_BAKE 并行）
#   先构建共享基础镜像 jonex/python-base:local 并 --load 进本地镜像库，
#   再用 COMPOSE_BAKE=1 委托 buildx bake 并行构建 deploy-* 镜像。
#   7 个后端服务通过 docker-compose.yml 的 additional_contexts 复用 base。
# ------------------------------------------------------------
build-python-base: ## 构建共享基础镜像 jonex/python-base:local（被 7 个后端服务复用）
	@echo "=== 构建共享基础镜像 $(PYTHON_BASE_TAG) ==="
	cd deploy && docker buildx build --load -t $(PYTHON_BASE_TAG) -f docker/python-base.Dockerfile \
		--build-arg USE_OVERSEAS_MIRROR=$(USE_OVERSEAS_MIRROR) ..

build: build-python-base ## 构建本地 Docker 联调镜像（共享 base + 并行 bake）
	cd deploy && DOCKER_BUILDKIT=1 COMPOSE_BAKE=1 BUILDX_BAKE_ENTITLEMENTS_FS=0 $(COMPOSE) $(COMPOSE_LOCAL_FILES) build

build-local: build ## 别名: 构建本地 Docker 镜像

build-overseas: ## 构建本地 Docker 镜像（国外源：官方 pypi / npm / debian 源）
	$(MAKE) build USE_OVERSEAS_MIRROR=true

build-gpu: build-python-base ## 构建 GPU Docker 镜像（Windows/Linux/服务器，需 NVIDIA GPU）
	cd deploy && DOCKER_BUILDKIT=1 COMPOSE_BAKE=1 BUILDX_BAKE_ENTITLEMENTS_FS=0 $(COMPOSE) $(COMPOSE_GPU_FILES) build

build-prod: build-python-base ## 构建生产 Docker 镜像（不含 GPU）
	cd deploy && DOCKER_BUILDKIT=1 COMPOSE_BAKE=1 BUILDX_BAKE_ENTITLEMENTS_FS=0 $(COMPOSE) $(COMPOSE_LOCAL_FILES) build

build-server: build-prod ## 别名: 构建生产 Docker 镜像

build-infra: ## 构建基础设施镜像（通常会直接拉取）
	$(DOCKER_BASE) build $(MIDDLEWARE_SERVICES)

build-rag: ## 构建 RAG 服务镜像（不依赖 python-base）
	cd deploy && DOCKER_BUILDKIT=1 COMPOSE_BAKE=1 BUILDX_BAKE_ENTITLEMENTS_FS=0 $(COMPOSE) $(COMPOSE_LOCAL_FILES) build $(RAG_SERVICES)

build-backend: build-python-base ## 构建后端镜像
	cd deploy && DOCKER_BUILDKIT=1 COMPOSE_BAKE=1 BUILDX_BAKE_ENTITLEMENTS_FS=0 $(COMPOSE) $(COMPOSE_LOCAL_FILES) build $(BACKEND_SERVICES)

build-frontend: ## 构建前端镜像（不依赖 python-base）
	cd deploy && DOCKER_BUILDKIT=1 COMPOSE_BAKE=1 BUILDX_BAKE_ENTITLEMENTS_FS=0 $(COMPOSE) $(COMPOSE_LOCAL_FILES) build $(FRONTEND_SERVICES)

build-service: _require-service build-python-base ## 构建指定服务: make build-service SERVICE=gateway
	cd deploy && DOCKER_BUILDKIT=1 COMPOSE_BAKE=1 BUILDX_BAKE_ENTITLEMENTS_FS=0 $(COMPOSE) $(COMPOSE_LOCAL_FILES) build $(SERVICE)

build-sidecar: ## 兼容旧命令: 构建 Sidecar
	@$(MAKE) build-service SERVICE=sidecar

build-knowledge-base: ## 兼容旧命令: 构建知识库服务
	@$(MAKE) build-service SERVICE=knowledge-base-service

build-business-domain: ## 兼容旧命令: 构建业务领域服务
	@$(MAKE) build-service SERVICE=business-domain-service

build-platform: ## 兼容旧命令: 构建平台管理服务
	@$(MAKE) build-service SERVICE=platform-service

# ------------------------------------------------------------
# Docker 启动/停止
# ------------------------------------------------------------
up: ## 启动本地 Docker 部署（加载 override，暴露调试端口）
	@echo "=== 本地 Docker 部署 ==="
	$(DOCKER_OVERRIDE) up -d
	@echo "服务启动中: make ps / make logs"

up-local: up ## 别名: 本地 Docker 部署
up-detached: up ## 兼容旧命令: 后台启动本地 Docker 部署

up-gpu: ## GPU Docker 部署（Windows/Linux/服务器，需 NVIDIA GPU）
	@echo "=== GPU Docker 部署 ==="
	$(DOCKER_GPU) up -d

up-server: ## 服务器/生产部署（不含 GPU，仅 docker-compose.yml）
	@echo "=== 生产 Docker 部署 ==="
	$(DOCKER_BASE) up -d

up-prod: up-server ## 别名: 服务器/生产部署

up-infra: ## 只启动中间件
	$(DOCKER_OVERRIDE) up -d $(MIDDLEWARE_SERVICES)

up-rag: ## 只启动 RAG 服务
	$(DOCKER_OVERRIDE) up -d $(RAG_SERVICES)

up-backend: ## 只启动后端服务
	$(DOCKER_OVERRIDE) up -d $(BACKEND_SERVICES)

up-frontend: ## 只启动前端服务
	$(DOCKER_OVERRIDE) up -d $(FRONTEND_SERVICES)

up-service: _require-service ## 启动指定服务: make up-service SERVICE=gateway
	$(DOCKER_OVERRIDE) up -d $(SERVICE)

up-sidecar: ## 兼容旧命令: 只启动 Sidecar
	@$(MAKE) up-service SERVICE=sidecar

up-knowledge-base: ## 兼容旧命令: 只启动知识库服务
	@$(MAKE) up-service SERVICE=knowledge-base-service

up-business-domain: ## 兼容旧命令: 只启动业务领域服务
	@$(MAKE) up-service SERVICE=business-domain-service

up-platform: ## 兼容旧命令: 只启动平台管理服务
	@$(MAKE) up-service SERVICE=platform-service

down: down-local ## 停止并删除本地 Docker 部署

down-local: ## 停止并删除本地 Docker 部署
	$(DOCKER_OVERRIDE) down

down-gpu: ## 停止并删除 GPU Docker 部署
	$(DOCKER_GPU) down

down-server: ## 停止并删除服务器/生产部署
	$(DOCKER_BASE) down

down-prod: down-server ## 别名: 停止并删除服务器/生产部署

down-v: ## 停止全部服务并删除数据卷（慎用）
	@echo "警告: 这会删除数据库、Redis、MinIO、Milvus 等数据卷。"
	@printf "确认继续? (y/N): "; read -r confirm; [ "$$confirm" = "y" ] || exit 1
	$(DOCKER_OVERRIDE) down -v

stop: ## 停止本地 Docker 服务（保留容器）
	$(DOCKER_OVERRIDE) stop

stop-service: _require-service ## 停止指定服务（保留容器）: make stop-service SERVICE=gateway
	$(DOCKER_OVERRIDE) stop $(SERVICE)

down-service: _require-service ## 停止并删除指定服务容器: make down-service SERVICE=gateway
	$(DOCKER_OVERRIDE) stop $(SERVICE)
	$(DOCKER_OVERRIDE) rm -f $(SERVICE)

down-gateway: ## 兼容旧命令: 停止并删除 Gateway 容器
	@$(MAKE) down-service SERVICE=gateway

down-sidecar: ## 兼容旧命令: 停止并删除 Sidecar 容器
	@$(MAKE) down-service SERVICE=sidecar

down-frontend: ## 兼容旧命令: 停止并删除全部前端容器
	$(DOCKER_OVERRIDE) stop $(FRONTEND_SERVICES)
	$(DOCKER_OVERRIDE) rm -f $(FRONTEND_SERVICES)

down-knowledge-base: ## 兼容旧命令: 停止并删除知识库服务容器
	@$(MAKE) down-service SERVICE=knowledge-base-service

down-business-domain: ## 兼容旧命令: 停止并删除业务领域服务容器
	@$(MAKE) down-service SERVICE=business-domain-service

down-platform: ## 兼容旧命令: 停止并删除平台管理服务容器
	@$(MAKE) down-service SERVICE=platform-service

restart: ## 重启本地 Docker 全部服务
	$(DOCKER_OVERRIDE) restart

restart-gpu: ## 重启 GPU Docker 全部服务
	$(DOCKER_GPU) restart

restart-server: ## 重启服务器/生产全部服务
	$(DOCKER_BASE) restart

restart-prod: restart-server ## 别名: 重启服务器/生产全部服务

restart-service: _require-service ## 重启指定服务: make restart-service SERVICE=gateway
	$(DOCKER_OVERRIDE) restart $(SERVICE)

recreate-service: _require-service ## 强制重建容器: make recreate-service SERVICE=gateway
	$(DOCKER_OVERRIDE) up -d --force-recreate $(SERVICE)

rebuild-service: _require-service build-python-base ## 重新构建并启动指定服务: make rebuild-service SERVICE=core-business-frontend
	cd deploy && DOCKER_BUILDKIT=1 COMPOSE_BAKE=1 BUILDX_BAKE_ENTITLEMENTS_FS=0 $(COMPOSE) $(COMPOSE_OVERRIDE_FILES) build --no-cache $(SERVICE)
	docker image prune -f --filter "dangling=true"
	$(DOCKER_OVERRIDE) up -d --force-recreate $(SERVICE)

# ------------------------------------------------------------
# Docker 查看
# ------------------------------------------------------------
ps: ## 查看本地 Docker 服务状态
	$(DOCKER_OVERRIDE) ps

ps-gpu: ## 查看 GPU Docker 服务状态
	$(DOCKER_GPU) ps

ps-server: ## 查看服务器/生产服务状态
	$(DOCKER_BASE) ps

ps-prod: ps-server ## 别名: 查看服务器/生产服务状态

logs: ## 查看本地 Docker 全部日志
	$(DOCKER_OVERRIDE) logs -f --tail=100

logs-gpu: ## 查看 GPU Docker 全部日志
	$(DOCKER_GPU) logs -f --tail=100

logs-server: ## 查看服务器/生产全部日志
	$(DOCKER_BASE) logs -f --tail=100

logs-prod: logs-server ## 别名: 查看服务器/生产全部日志

logs-service: _require-service ## 查看指定服务日志: make logs-service SERVICE=gateway
	$(DOCKER_OVERRIDE) logs -f --tail=100 $(SERVICE)

logs-gateway: ## 查看 Gateway 日志
	@$(MAKE) logs-service SERVICE=gateway

logs-sidecar: ## 查看 Sidecar 日志
	@$(MAKE) logs-service SERVICE=sidecar

logs-knowledge-base: ## 查看知识库服务日志
	@$(MAKE) logs-service SERVICE=knowledge-base-service

logs-business-domain: ## 查看业务领域服务日志
	@$(MAKE) logs-service SERVICE=business-domain-service

logs-platform: ## 查看平台管理服务日志
	@$(MAKE) logs-service SERVICE=platform-service

logs-mcp-server: ## 查看 MCP Server 日志
	@$(MAKE) logs-service SERVICE=mcp-server

logs-lightrag: ## 查看 LightRAG 日志
	@$(MAKE) logs-service SERVICE=lightrag

# ── openkb ──
up-openkb: ## 启动 openkb 服务
	$(DOCKER_OVERRIDE) up -d openkb

logs-openkb: ## 查看 openkb 日志
	@$(MAKE) logs-service SERVICE=openkb

exec-openkb: ## 进入 openkb 容器
	$(DOCKER_OVERRIDE) exec openkb bash

rebuild-openkb: ## 重建 openkb 镜像
	cd deploy && DOCKER_BUILDKIT=1 COMPOSE_BAKE=1 BUILDX_BAKE_ENTITLEMENTS_FS=0 $(COMPOSE) $(COMPOSE_LOCAL_FILES) build --no-cache openkb

logs-postgres: ## 查看 Postgres 日志
	@$(MAKE) logs-service SERVICE=postgres

logs-redis: ## 查看 Redis 日志
	@$(MAKE) logs-service SERVICE=redis

logs-milvus: ## 查看 Milvus 日志
	@$(MAKE) logs-service SERVICE=milvus

logs-etcd: ## 查看 Etcd 日志
	@$(MAKE) logs-service SERVICE=etcd

logs-minio: ## 查看 MinIO 日志
	@$(MAKE) logs-service SERVICE=minio

logs-infra: ## 查看中间件日志
	$(DOCKER_OVERRIDE) logs -f --tail=100 $(MIDDLEWARE_SERVICES)

logs-rag: ## 查看 RAG 日志
	$(DOCKER_OVERRIDE) logs -f --tail=100 $(RAG_SERVICES)

logs-backend: ## 查看后端日志
	$(DOCKER_OVERRIDE) logs -f --tail=100 $(BACKEND_SERVICES)

logs-frontend: ## 查看前端日志
	$(DOCKER_OVERRIDE) logs -f --tail=100 $(FRONTEND_SERVICES)

# ------------------------------------------------------------
# 性能耗时日志查询（摄入链路埋点，详见 docs/ingestion-timing-metrics-design.md §10）
#   默认查最近 PERF_TAIL 行历史日志（非 follow），可用 PERF_TAIL=N 调整。
#   实时跟踪某条 timing 日志可直接: make logs-service SERVICE=atomic-rag
# ------------------------------------------------------------
perf: ## 汇总查看平台侧摄入耗时（ingest_timing + reconcile_timing）
	@echo "=== ingest_timing (atomic-rag, worker 分阶段耗时) ==="
	@$(MAKE) --no-print-directory perf-ingest
	@echo ""
	@echo "=== reconcile_timing (knowledge-base-service, 入图库/端到端耗时) ==="
	@$(MAKE) --no-print-directory perf-reconcile

perf-ingest: ## worker 分阶段耗时 ingest_timing（atomic-rag）: make perf-ingest [PERF_TAIL=5000]
	$(DOCKER_OVERRIDE) logs --tail=$(PERF_TAIL) atomic-rag | grep ingest_timing || true

perf-reconcile: ## 对账入图库耗时 reconcile_timing（knowledge-base-service）
	$(DOCKER_OVERRIDE) logs --tail=$(PERF_TAIL) knowledge-base-service | grep reconcile_timing || true

perf-chunk: ## LightRAG 内部 chunk_timing（lightrag，拆 extract/merge/persist）
	$(DOCKER_OVERRIDE) logs --tail=$(PERF_TAIL) lightrag | grep chunk_timing || true

perf-thinking: ## 关思考注入 thinking.disabled（llm-gateway）
	$(DOCKER_OVERRIDE) logs --tail=$(PERF_TAIL) llm-gateway | grep "thinking.disabled" || true

perf-extract: ## 抽取场景调用 lightrag_extract（llm-gateway，看 latency/token）
	$(DOCKER_OVERRIDE) logs --tail=$(PERF_TAIL) llm-gateway | grep lightrag_extract || true

perf-search: ## 本体检索 RAG 线路耗时 ontology_search_timing（knowledge-base-service，多库检索/融合）
	$(DOCKER_OVERRIDE) logs --tail=$(PERF_TAIL) knowledge-base-service | grep ontology_search_timing || true

perf-audit: ## 审计表耗时 audit_logs.duration_ms（postgres，摄入终态）
	$(DOCKER_OVERRIDE) exec postgres psql -U $(DB_USERNAME) -d $(DB_NAME) -c "SELECT created_at, action, outcome, duration_ms, resource_id, request_params FROM platform.audit_logs WHERE action IN ('document.parse_done','document.parse_failed','document.parse_recover') ORDER BY created_at DESC LIMIT 20;"

# LLM 计量可读汇总：DIM=doc(默认)/trace/daily；行数 PERF_LLM_LIMIT 默认 30
PERF_LLM_DIM ?= doc
PERF_LLM_LIMIT ?= 30

perf-llm: ## LLM 计量可读汇总: make perf-llm [PERF_LLM_DIM=doc|trace|daily] [PERF_LLM_LIMIT=30]
	$(DOCKER_OVERRIDE) exec postgres psql -U $(DB_USERNAME) -d $(DB_NAME) -c "SELECT * FROM metering.v_llm_usage_by_$(PERF_LLM_DIM) ORDER BY total_tokens DESC NULLS LAST LIMIT $(PERF_LLM_LIMIT);"

perf-llm-detail: ## LLM 计量明细可读视图（关联 kb/doc 名称 + 本地时区）: make perf-llm-detail [PERF_LLM_LIMIT=30]
	$(DOCKER_OVERRIDE) exec postgres psql -U $(DB_USERNAME) -d $(DB_NAME) -c "SELECT * FROM metering.v_llm_usage_detail ORDER BY created_local DESC LIMIT $(PERF_LLM_LIMIT);"

# LLM 计量历史治理（Phase 3）：先 rollup 到 llm_usage_daily，再（可选）清理超期明细。
# 默认 dry-run 只统计将删行数；CONFIRM=1 才真正删除。保留期 METERING_RETENTION_DAYS（默认 90）。
metering-rollup: ## rollup 汇总 + 清理超期明细: make metering-rollup [METERING_RETENTION_DAYS=90] [CONFIRM=1]
	@echo "=== 1) rollup → metering.llm_usage_daily（整天重聚合，幂等）==="
	$(DOCKER_OVERRIDE) exec postgres psql -U $(DB_USERNAME) -d $(DB_NAME) -c "INSERT INTO metering.llm_usage_daily (day_local, tenant_id, scene, model, call_count, prompt_tokens, completion_tokens, total_tokens, avg_latency_ms) SELECT (created_at AT TIME ZONE 'Asia/Shanghai')::date, tenant_id, scene, model, SUM(call_count), SUM(prompt_tokens), SUM(completion_tokens), SUM(total_tokens), ROUND(AVG(latency_ms)) FROM metering.llm_usage_log GROUP BY 1, tenant_id, scene, model ON CONFLICT (day_local, tenant_id, scene, model) DO UPDATE SET call_count=EXCLUDED.call_count, prompt_tokens=EXCLUDED.prompt_tokens, completion_tokens=EXCLUDED.completion_tokens, total_tokens=EXCLUDED.total_tokens, avg_latency_ms=EXCLUDED.avg_latency_ms;"
ifeq ($(CONFIRM),1)
	@echo "=== 2) 清理超 $(METERING_RETENTION_DAYS) 天且已汇总的明细（CONFIRM=1，实际删除）==="
	$(DOCKER_OVERRIDE) exec postgres psql -U $(DB_USERNAME) -d $(DB_NAME) -c "DELETE FROM metering.llm_usage_log u WHERE u.created_at < now() - interval '$(METERING_RETENTION_DAYS) days' AND EXISTS (SELECT 1 FROM metering.llm_usage_daily d WHERE d.day_local=(u.created_at AT TIME ZONE 'Asia/Shanghai')::date AND d.tenant_id=u.tenant_id);"
else
	@echo "=== 2) (dry-run) 将清理以下行数（加 CONFIRM=1 实际删除）==="
	$(DOCKER_OVERRIDE) exec postgres psql -U $(DB_USERNAME) -d $(DB_NAME) -c "SELECT count(*) AS deletable_rows FROM metering.llm_usage_log u WHERE u.created_at < now() - interval '$(METERING_RETENTION_DAYS) days' AND EXISTS (SELECT 1 FROM metering.llm_usage_daily d WHERE d.day_local=(u.created_at AT TIME ZONE 'Asia/Shanghai')::date AND d.tenant_id=u.tenant_id);"
endif

# ------------------------------------------------------------
# Mac 本地开发: Docker 常驻依赖
# ------------------------------------------------------------
dev: dev-deps-up ## macOS/Linux 本机开发入口

dev-deps-up: ## 启动本地开发依赖（中间件 + RAG）
	$(DOCKER_OVERRIDE) up -d $(MIDDLEWARE_SERVICES) $(RAG_SERVICES)

dev-infra-up: ## 只启动中间件常驻服务
	$(DOCKER_OVERRIDE) up -d $(MIDDLEWARE_SERVICES)

dev-rag-up: ## 只启动 RAG 服务
	$(DOCKER_OVERRIDE) up -d $(RAG_SERVICES)

dev-deps-down: ## 停止本地开发依赖（保留容器和数据卷）
	$(DOCKER_OVERRIDE) stop $(MIDDLEWARE_SERVICES) $(RAG_SERVICES)

dev-deps-logs: ## 查看本地开发依赖日志
	$(DOCKER_OVERRIDE) logs -f --tail=100 $(MIDDLEWARE_SERVICES) $(RAG_SERVICES)

# ------------------------------------------------------------
# 宿主机单服务调试: debug compose
# ------------------------------------------------------------

docker-local-build: build ## 宿主机单服务调试: 构建镜像
docker-local-up: ## 宿主机单服务调试: 启动整套服务（加载 debug 覆盖）
	@echo "=== 宿主机单服务调试 ==="
	$(DOCKER_DEV) up -d

docker-local-down: ## 宿主机单服务调试: 停止整套服务
	$(DOCKER_DEV) down

docker-local-ps: ## 宿主机单服务调试: 查看状态
	$(DOCKER_DEV) ps

docker-local-logs: ## 宿主机单服务调试: 查看日志
	$(DOCKER_DEV) logs -f --tail=100

docker-local-restart: ## 宿主机单服务调试: 重启整套服务
	$(DOCKER_DEV) restart

docker-local-up-service: _require-service ## 宿主机单服务调试: 启动单服务
	$(DOCKER_DEV) up -d $(SERVICE)

docker-local-down-service: _require-service ## 宿主机单服务调试: 停止并删除单服务
	$(DOCKER_DEV) stop $(SERVICE)
	$(DOCKER_DEV) rm -f $(SERVICE)

docker-local-logs-service: _require-service ## 宿主机单服务调试: 查看单服务日志
	$(DOCKER_DEV) logs -f --tail=100 $(SERVICE)

docker-local-restart-service: _require-service ## 宿主机单服务调试: 重启单服务
	$(DOCKER_DEV) restart $(SERVICE)

docker-local-recreate-service: _require-service ## 宿主机单服务调试: 强制重建单服务容器
	$(DOCKER_DEV) up -d --force-recreate $(SERVICE)

docker-local-rebuild-service: _require-service build-python-base ## 宿主机单服务调试: 重建并启动单服务
	cd deploy && DOCKER_BUILDKIT=1 COMPOSE_BAKE=1 BUILDX_BAKE_ENTITLEMENTS_FS=0 $(COMPOSE) $(COMPOSE_DEV_FILES) build --no-cache $(SERVICE)
	docker image prune -f --filter "dangling=true"
	$(DOCKER_DEV) up -d --force-recreate $(SERVICE)

docker-gpu-build: build-gpu ## GPU/服务器 Docker: 构建镜像

docker-gpu-up: up-gpu ## GPU/服务器 Docker: 启动整套服务

docker-gpu-down: down-gpu ## GPU/服务器 Docker: 停止整套服务

docker-gpu-ps: ps-gpu ## GPU/服务器 Docker: 查看状态

docker-gpu-logs: logs-gpu ## GPU/服务器 Docker: 查看日志

docker-gpu-restart: restart-gpu ## GPU/服务器 Docker: 重启整套服务

docker-gpu-up-service: _require-service ## GPU/服务器 Docker: 启动单服务
	$(DOCKER_GPU) up -d $(SERVICE)

docker-gpu-down-service: _require-service ## GPU/服务器 Docker: 停止并删除单服务
	$(DOCKER_GPU) stop $(SERVICE)
	$(DOCKER_GPU) rm -f $(SERVICE)

docker-gpu-logs-service: _require-service ## GPU/服务器 Docker: 查看单服务日志
	$(DOCKER_GPU) logs -f --tail=100 $(SERVICE)

docker-gpu-restart-service: _require-service ## GPU/服务器 Docker: 重启单服务
	$(DOCKER_GPU) restart $(SERVICE)

docker-gpu-rebuild-service: _require-service build-python-base ## GPU/服务器 Docker: 重建并启动单服务
	cd deploy && DOCKER_BUILDKIT=1 COMPOSE_BAKE=1 BUILDX_BAKE_ENTITLEMENTS_FS=0 $(COMPOSE) $(COMPOSE_GPU_FILES) build --no-cache $(SERVICE)
	docker image prune -f --filter "dangling=true"
	$(DOCKER_GPU) up -d --force-recreate $(SERVICE)


# ------------------------------------------------------------
# Mac 本地开发: 前端 TypeScript workspace
# ------------------------------------------------------------
frontends-install: ## 安装/同步前端 workspace 依赖
	$(PNPM) -C $(FRONTENDS_DIR) install

dev-gateway: ## 启动 Dev Gateway 统一入口: http://localhost:8080
	$(PNPM) -C $(FRONTENDS_DIR) run dev:gateway

dev-frontend: ## 一键启动 shell + 三个新 TS 子应用
	$(PNPM) -C $(FRONTENDS_DIR) -r --parallel $(MAIN_FRONTEND_FILTERS) run dev

dev-frontend-all: ## 启动 Dev Gateway + 全部 4 个前端子应用（Ctrl+C 停止全部）
	@trap 'kill 0' EXIT; \
	$(PNPM) -C $(FRONTENDS_DIR) run dev:gateway & \
	$(PNPM) -C $(FRONTENDS_DIR) -r --parallel $(MAIN_FRONTEND_FILTERS) run dev

dev-frontend-shell: ## 启动 shell
	$(PNPM) -C $(FRONTENDS_DIR) --filter $(SHELL_APP) run dev

dev-frontend-core: ## 启动业务领域管理
	$(PNPM) -C $(FRONTENDS_DIR) --filter $(CORE_APP) run dev


dev-frontend-platform: ## 启动平台管理
	$(PNPM) -C $(FRONTENDS_DIR) --filter $(PLATFORM_APP) run dev

dev-frontend-ecosystem: ## 启动生态管理
	$(PNPM) -C $(FRONTENDS_DIR) --filter $(ECOSYSTEM_APP) run dev

preview-core: ## 构建并 preview 业务领域管理
	$(PNPM) -C $(FRONTENDS_DIR) --filter $(CORE_APP) run build
	$(PNPM) -C $(FRONTENDS_DIR) --filter $(CORE_APP) run preview


preview-platform: ## 构建并 preview 平台管理
	$(PNPM) -C $(FRONTENDS_DIR) --filter $(PLATFORM_APP) run build
	$(PNPM) -C $(FRONTENDS_DIR) --filter $(PLATFORM_APP) run preview

preview-ecosystem: ## 构建并 preview 生态管理
	$(PNPM) -C $(FRONTENDS_DIR) --filter $(ECOSYSTEM_APP) run build
	$(PNPM) -C $(FRONTENDS_DIR) --filter $(ECOSYSTEM_APP) run preview

preview-all: ## 构建并 preview 三个新 TS 子应用
	$(PNPM) -C $(FRONTENDS_DIR) -r $(MAIN_FRONTEND_FILTERS) run build
	$(PNPM) -C $(FRONTENDS_DIR) -r --parallel $(MAIN_FRONTEND_FILTERS) run preview

# ------------------------------------------------------------
# 常用单服务别名
# ------------------------------------------------------------
rebuild-gateway:
	@$(MAKE) rebuild-service SERVICE=gateway

rebuild-sidecar:
	@$(MAKE) rebuild-service SERVICE=sidecar

rebuild-knowledge-base:
	@$(MAKE) rebuild-service SERVICE=knowledge-base-service

rebuild-business-domain:
	@$(MAKE) rebuild-service SERVICE=business-domain-service

rebuild-platform:
	@$(MAKE) rebuild-service SERVICE=platform-service

rebuild-frontend-gateway:
	@$(MAKE) rebuild-service SERVICE=frontend-gateway

rebuild-shell-frontend:
	@$(MAKE) rebuild-service SERVICE=shell-frontend

rebuild-core-business-frontend:
	@$(MAKE) rebuild-service SERVICE=core-business-frontend

rebuild-platform-management-frontend:
	@$(MAKE) rebuild-service SERVICE=platform-management-frontend

rebuild-ecosystem-management-frontend:
	@$(MAKE) rebuild-service SERVICE=ecosystem-management-frontend

restart-gateway:
	@$(MAKE) restart-service SERVICE=gateway

restart-sidecar:
	@$(MAKE) restart-service SERVICE=sidecar

restart-knowledge-base:
	@$(MAKE) restart-service SERVICE=knowledge-base-service

restart-business-domain:
	@$(MAKE) restart-service SERVICE=business-domain-service

restart-platform:
	@$(MAKE) restart-service SERVICE=platform-service

restart-frontend:
	$(DOCKER_OVERRIDE) restart $(FRONTEND_SERVICES)

restart-frontend-gateway:
	@$(MAKE) restart-service SERVICE=frontend-gateway

restart-shell-frontend:
	@$(MAKE) restart-service SERVICE=shell-frontend

restart-core-business-frontend:
	@$(MAKE) restart-service SERVICE=core-business-frontend

restart-platform-management-frontend:
	@$(MAKE) restart-service SERVICE=platform-management-frontend

restart-ecosystem-management-frontend:
	@$(MAKE) restart-service SERVICE=ecosystem-management-frontend

scale-knowledge-base: ## 扩缩知识库服务: make scale-knowledge-base N=3
	$(DOCKER_OVERRIDE) up -d --scale knowledge-base-service=$(N) knowledge-base-service

# ------------------------------------------------------------
# 常用基础服务别名
# ------------------------------------------------------------
up-postgres:
	$(DOCKER_OVERRIDE) up -d postgres

up-redis:
	$(DOCKER_OVERRIDE) up -d redis

up-milvus:
	$(DOCKER_OVERRIDE) up -d etcd minio milvus

up-lightrag:
	$(DOCKER_OVERRIDE) up -d lightrag

pull-lightrag:
	$(DOCKER_GPU) pull lightrag

init-db: ## 初始化数据库
	$(DOCKER_OVERRIDE) exec postgres psql -U $(DB_USERNAME) -d $(DB_NAME) -f /docker-entrypoint-initdb.d/init.sql

db-migrate: ## 应用增量 DDL（update/*.sql 到运行中的 postgres，人工触发，交互确认）
	bash deploy/postgres/update/apply.sh

# 迁移已改为人工显式执行（不再由 make up 自动触发），迁移状态记录在
# public.schema_migrations 版本表：已应用文件跳过、失败不登记。发布前先
# `make db-migrate-dry` 预览待应用列表，再 `make db-migrate`（或 --yes）执行。
db-migrate-dry: ## 预览待应用迁移（不执行）
	bash deploy/postgres/update/apply.sh --dry-run

db-migrate-yes: ## 跳过确认直接执行待应用迁移（CI/自动化）
	bash deploy/postgres/update/apply.sh --yes

test: ## 运行基础健康检查
	$(DOCKER_OVERRIDE) ps
	@echo ""
	@echo "检查 API Gateway..."
	@curl -fsS http://localhost:8000/health || echo "API Gateway 未就绪或未启动"
	@echo ""
	@echo "检查 Sidecar..."
	@curl -fsS http://localhost:8001/health || echo "Sidecar 未就绪或未启动"

clean: ## 清理容器、数据卷和镜像（慎用）
	@echo "警告: 这会删除本项目容器、数据卷和相关镜像。"
	@printf "确认继续? (y/N): "; read -r confirm; [ "$$confirm" = "y" ] || exit 1
	$(DOCKER_OVERRIDE) down -v --rmi all
	docker image prune -f

# ------------------------------------------------------------
# 进入容器
# ------------------------------------------------------------
exec-postgres:
	$(DOCKER_OVERRIDE) exec postgres psql -U $(DB_USERNAME) -d $(DB_NAME)

exec-gateway:
	$(DOCKER_OVERRIDE) exec gateway bash

exec-sidecar:
	$(DOCKER_OVERRIDE) exec sidecar bash

exec-knowledge-base:
	$(DOCKER_OVERRIDE) exec knowledge-base-service bash

exec-business-domain:
	$(DOCKER_OVERRIDE) exec business-domain-service bash

exec-platform:
	$(DOCKER_OVERRIDE) exec platform-service bash

exec-lightrag:
	$(DOCKER_OVERRIDE) exec lightrag bash

exec-shell-frontend:
	$(DOCKER_OVERRIDE) exec shell-frontend sh

shell-postgres: exec-postgres
shell-gateway: exec-gateway
shell-sidecar: exec-sidecar
shell-knowledge-base: exec-knowledge-base
shell-business-domain: exec-business-domain
shell-platform: exec-platform
shell-lightrag: exec-lightrag
shell-frontend: exec-shell-frontend

# ------------------------------------------------------------
# 内部校验
# ------------------------------------------------------------
_require-service:
	@if [ -z "$(SERVICE)" ]; then \
		echo "请指定 SERVICE，例如:"; \
		echo "  make docker-local-restart-service SERVICE=gateway"; \
		echo "  make docker-local-rebuild-service SERVICE=core-business-frontend"; \
		exit 1; \
	fi
