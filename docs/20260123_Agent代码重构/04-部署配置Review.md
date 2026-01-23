# 部署配置 Review 报告

## 📋 Review 概述

**Review 日期**: 2026-01-23
**范围**: Dockerfile, 部署脚本, requirements.txt
**状态**: ✅ 已修复

---

## 🔧 发现的问题和修复

### 1. Dockerfile 问题

#### 1.1 根目录 Dockerfile（已删除）

**问题**：
- 这是原 monorepo 的 Dockerfile
- 包含 Frontend 构建、Nginx 等与 Agent 无关的内容
- 引用不存在的目录：`frontend/`, `backend/`, `config/`

**解决方案**：删除此文件

#### 1.2 scripts/Dockerfile（已修复）

**原问题**：
```dockerfile
# 错误的路径引用
COPY backend/ ./backend/
COPY config/ ./config/
COPY run.py ./run.py

# 错误的启动命令
CMD ["opentelemetry-instrument", "python", "-m", "backend.agent.agent_runtime"]
```

**修复后**：
```dockerfile
# 正确的路径引用
COPY costq_agents/ ./costq_agents/
COPY config_runtime/ ./config_runtime/

# 正确的启动命令
CMD ["opentelemetry-instrument", "python", "-m", "costq_agents.agent.runtime"]
```

---

### 2. 构建脚本问题

#### 2.1 scripts/01-build_and_push.sh（已修复）

**原问题**：
```bash
# 错误的 Dockerfile 路径
-f deployment/agentcore/Dockerfile \

# 硬编码的目录切换
cd ../..
```

**修复后**：
```bash
# 正确的 Dockerfile 路径
-f scripts/Dockerfile \

# 自动计算项目根目录
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${PROJECT_ROOT}"
```

---

### 3. requirements.txt 优化

#### 3.1 移除冗余依赖

| 依赖包 | 状态 | 理由 |
|--------|------|------|
| `aiohttp` | ❌ 移除 | 代码中无任何引用 |
| `cachetools` | ❌ 移除 | 代码中无任何引用 |

#### 3.2 添加缺失依赖

| 依赖包 | 状态 | 理由 |
|--------|------|------|
| `google-cloud-compute` | ✅ 添加 | `compute_client.py` 使用（虽标记废弃但仍被导入） |

#### 3.3 依赖使用验证

| 依赖包 | 使用情况 |
|--------|----------|
| `bedrock-agentcore` | ✅ `agent/runtime.py` |
| `strands-agents[otel]` | ✅ `agent/manager.py` |
| `strands-agents-tools` | ✅ `agent/manager.py` (calculator) |
| `boto3/botocore` | ✅ 多处使用 |
| `pydantic/pydantic-settings` | ✅ `config/settings.py` |
| `sqlalchemy` | ✅ `database/connection.py` |
| `psycopg2-binary` | ✅ PostgreSQL 驱动 |
| `httpx` | ✅ `services/streamable_http_sigv4.py` |
| `mcp` | ✅ `mcp/mcp_manager.py`, 所有 MCP Server |
| `mcp-proxy` | ✅ MCP 远程连接 |
| `cryptography` | ✅ `services/credential_manager.py` |
| `google-cloud-*` | ✅ `mcp/gcp_cost_mcp_server/` |
| `python-dotenv` | ✅ 本地开发环境变量加载 |

---

## 📁 当前部署文件结构

```
costq-agents/
├── scripts/
│   ├── Dockerfile              # ✅ AgentCore Runtime 镜像
│   ├── 01-build_and_push.sh    # ✅ 构建并推送镜像到 ECR
│   ├── 02-update_runtime.sh    # ✅ 更新 AgentCore Runtime
│   └── 03-verify_deployment.sh # ✅ 验证部署状态
├── config_runtime/
│   └── mcp_config.json         # ✅ MCP 配置
├── requirements.txt            # ✅ Python 依赖
└── pyproject.toml              # ✅ 项目配置
```

---

## 🚀 部署流程

### Step 1: 构建并推送镜像

```bash
cd costq-agents/scripts
./01-build_and_push.sh
```

**输出**：
- 构建 ARM64 Docker 镜像
- 推送到 ECR: `000451883532.dkr.ecr.ap-northeast-1.amazonaws.com/costq-agentcore`
- 打标签：`latest` + `v{timestamp}`

### Step 2: 更新 Runtime

```bash
# 更新开发环境（默认）
./02-update_runtime.sh v20260123-160000

# 更新生产环境（指定 Runtime ID）
./02-update_runtime.sh v20260123-160000 cosq_agentcore_runtime_production-xxxxx
```

**功能**：
- 保留所有环境变量
- 保留网络配置
- 保留 IAM Role
- 仅更新镜像 URI

### Step 3: 验证部署

```bash
./03-verify_deployment.sh [runtime-id]
```

**检查项**：
- Runtime 状态（READY）
- 最近的调用日志
- 错误日志
- 关键配置（ENCRYPTION_KEY, MEMORY_RESOURCE_ID, RDS_SECRET_NAME）

---

## ⚠️ 注意事项

### 1. 环境变量配置

Runtime 需要以下环境变量（通过 AgentCore 控制台配置）：

| 变量名 | 说明 | 必需 |
|--------|------|------|
| `ENCRYPTION_KEY` | Fernet 加密密钥 | ✅ |
| `RDS_SECRET_NAME` | RDS 密钥名称 | ✅ |
| `MEMORY_RESOURCE_ID` | AgentCore Memory ID | 可选 |
| `AWS_REGION` | AWS 区域 | ✅ |
| `BEDROCK_REGION` | Bedrock 区域 | ✅ |
| `BEDROCK_CROSS_ACCOUNT_ROLE_ARN` | Bedrock 跨账号 Role | 可选 |
| `COSTQ_AWS_MCP_SERVERS_GATEWAY_URL` | Gateway MCP URL | 可选 |

### 2. 网络配置

Runtime 部署在 VPC 内，需要：
- 私有子网访问
- Security Group 配置
- VPC Endpoints（Bedrock, Secrets Manager, ECR）

### 3. IAM 权限

Runtime IAM Role 需要：
- Bedrock InvokeModel
- Secrets Manager GetSecretValue
- S3 读写（Memory）
- CloudWatch Logs

---

## ✅ Review 结论

| 检查项 | 状态 | 说明 |
|--------|------|------|
| Dockerfile | ✅ 已修复 | 路径和命令已更新 |
| 构建脚本 | ✅ 已修复 | 路径自动计算 |
| requirements.txt | ✅ 已优化 | 移除冗余，添加缺失 |
| 部署流程 | ✅ 完整 | 三步部署流程 |

**建议**：可以正常进行 Docker 构建和部署

---

**Reviewer**: AI Assistant
**Date**: 2026-01-23
