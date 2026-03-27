# ============================================
# CostQ Agent Runtime Dockerfile
# ============================================
# 用途：构建 AgentCore Runtime 镜像
# 部署目标：AWS AgentCore Runtime (ARM64)
# ============================================

FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim
WORKDIR /app

# All environment variables in one layer
ENV UV_SYSTEM_PYTHON=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_NO_PROGRESS=1 \
    PYTHONUNBUFFERED=1 \
    DOCKER_CONTAINER=1 \
    ENVIRONMENT=production

# ========== 安装系统依赖 ==========
# 注意：uvx 已经包含在基础镜像 ghcr.io/astral-sh/uv 中，无需单独安装
# 只需安装编译工具和系统库用于构建 Python 包
RUN apt-get update && apt-get install -y --no-install-recommends \
    # 编译工具（用于某些 Python 包的 C 扩展）
    build-essential \
    gcc \
    g++ \
    # 系统库（用于 cryptography, psycopg2 等包）
    libssl-dev \
    libffi-dev \
    libpq-dev \
    # 工具包
    curl \
    ca-certificates \
    # 清理 apt 缓存减小镜像大小
    && rm -rf /var/lib/apt/lists/* \
    # 验证 uvx 可用（基础镜像自带）
    && uvx --version

# ========== 安装 Python 依赖 ==========
# 先复制 requirements.txt
COPY requirements.txt .

# 安装项目依赖
RUN uv pip install -r requirements.txt

# 安装 OpenTelemetry（用于日志追踪）
RUN uv pip install aws-opentelemetry-distro==0.12.2

# ========== 保持 root 用户运行 ==========
# Runtime 以 root 用户运行，与预装工具的用户一致
USER root

EXPOSE 9000
EXPOSE 8000
EXPOSE 8080

# ========== 复制应用代码 ==========
# 复制 Agent 代码包
COPY costq_agents/ ./costq_agents/

# 复制 MCP 配置文件
COPY config_runtime/ ./config_runtime/

# ========== 启动命令 ==========
# 使用 OpenTelemetry 自动注入启动 Agent Runtime
CMD ["opentelemetry-instrument", "python", "-m", "costq_agents.agent.runtime"]
