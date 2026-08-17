# ================================================================
# Botler Docker 镜像（多阶段构建）
#
# 阶段 1 frontend-builder：node 构建 React/Vite 前端 → dist
# 阶段 2 runtime：node + python 运行时
#   - claude CLI（npm 全局，执行器核心依赖）
#   - git（仓库工作区 reset/clean/push 依赖）
#   - 后端 Python 依赖（/opt/venv）
#
# 构建加速：pip 走清华源、npm 走 npmmirror（可用 --build-arg 覆盖）
# 国内无法访问 Docker Hub 时，可覆盖基础镜像前缀：
#   docker build --build-arg NODE_IMAGE=docker.m.daocloud.io/library/node:20-alpine \
#                --build-arg RUNTIME_IMAGE=docker.m.daocloud.io/library/node:20-bookworm-slim \
#                -t botler:latest .
# 用法: docker build -t botler:latest .
# ================================================================

# 基础镜像（可用 --build-arg 覆盖为国内镜像前缀）
ARG NODE_IMAGE=node:20-alpine
ARG RUNTIME_IMAGE=node:20-bookworm-slim

# ---------- 阶段 1：构建前端 ----------
FROM ${NODE_IMAGE} AS frontend-builder

ARG NPM_REGISTRY_URL=https://registry.npmmirror.com
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --registry=${NPM_REGISTRY_URL}
COPY frontend/ ./
RUN npm run build

# ---------- 阶段 2：运行时 ----------
FROM ${RUNTIME_IMAGE} AS runtime

ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ARG NPM_REGISTRY_URL=https://registry.npmmirror.com
ENV PIP_INDEX_URL=${PIP_INDEX_URL} \
    NPM_REGISTRY_URL=${NPM_REGISTRY_URL} \
    # 后端启动即生效的路径环境变量（与 pm2/systemd 部署同约定）
    BOTLER_CONFIG=/app/backend/config.yaml \
    BOTLER_DB=/app/backend/botler.db \
    PYTHONPATH=/app/backend \
    PYTHONUNBUFFERED=1 \
    # 部署时区固定亚洲/上海（tzdata 见下方 apt 安装）
    TZ=Asia/Shanghai

# git: 执行器工作区必需；curl: healthcheck；python3-venv: 后端虚拟环境；
# tzdata: 时区库（TZ=Asia/Shanghai 依赖它解析，日志/时间戳按上海时间）
RUN apt-get update && apt-get install -y --no-install-recommends \
        git curl ca-certificates python3 python3-venv python3-pip tzdata \
    && ln -snf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime \
    && echo Asia/Shanghai > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先 COPY requirements 单独一层，依赖变更才重装（层缓存）
COPY backend/requirements.txt backend/requirements.txt
RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir -r backend/requirements.txt -i ${PIP_INDEX_URL} \
    # claude CLI：Claude Code 执行器核心依赖
    && npm install -g @anthropic-ai/claude-code --registry=${NPM_REGISTRY_URL}

# dsh 引擎 SDK（issue #112：deepseek-harness 依赖纳入 Docker 部署，
# 原为可选依赖需手动安装）。清华 pip 镜像未同步 rc 预发布版（仅有
# 0.0.0.dev0 占位），必须用阿里镜像；rc 为预发布版本，安装须显式
# 写全版本号。镜像源可用 DSH_INDEX_URL build arg 覆盖（内网代理场景）。
# 构建期 import 校验：SDK 装不上时镜像构建直接失败（fail fast）。
ARG DSH_INDEX_URL=https://mirrors.aliyun.com/pypi/simple
RUN /opt/venv/bin/pip install --no-cache-dir "deepseek-harness-sdk==0.1.0rc6" \
        -i ${DSH_INDEX_URL} \
    && /opt/venv/bin/python -c "from deepseek_harness import DeepSeekHarness"

COPY backend/ ./backend/
# 前端构建产物由 FastAPI 静态托管（main.py 检测 frontend/dist）
COPY --from=frontend-builder /build/dist/ ./frontend/dist/

# 运行时目录：默认挂载卷覆盖，先创建保证裸启动可用
RUN mkdir -p /app/workspace /app/logs

# hermes 引擎 SDK（issue #171：hermes agent SDK 进程内集成）。
# hermes-agent 以源码分发（PyPI 无 wheel），Docker 构建期无法访问宿主机
# 源码（NAS 上经 docker-compose 只读挂载 /opt/hermes/hermes-agent），因此
# editable 安装放在容器启动时由 docker-entrypoint.sh 幂等执行（挂载了
# 源码才装，未挂载跳过并告警——与 dsh SDK 未装同语义，启动不失败）。
# 源码路径可用 HERMES_SOURCE_DIR 环境变量覆盖。
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/api/health || exit 1

# 与 pm2/systemd 部署相同的启动命令（经 entrypoint 先处理 hermes SDK）
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["/opt/venv/bin/uvicorn", "botler.main:app", "--host", "0.0.0.0", "--port", "8000"]
