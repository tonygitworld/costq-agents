# CostQ Agents

CostQ Agent 运行时和 MCP 服务器，从单体仓库中独立拆分。

## 📋 项目说明

本仓库包含 CostQ 的 Agent 核心代码，用于部署到 AWS AgentCore Runtime。主要功能：

- **Agent Runtime** - AgentCore Runtime 入口，处理查询请求
- **Agent Manager** - 管理 Bedrock Model、Memory、Prompt Caching
- **MCP Manager** - 管理本地和 Gateway MCP 客户端
- **本地 MCP Servers** - 通用工具、告警管理、邮件发送、GCP 成本分析
- **服务层** - 凭证管理、IAM Role、数据库访问
- **Prompt 系统** - 模块化提示词管理

## 🚀 快速开始

### 环境要求

- Python >= 3.11
- PostgreSQL (RDS)
- AWS CLI 配置（本地开发）
- AWS IAM Role（生产环境）

### 安装依赖

```bash
pip install -r requirements.txt
```

### 环境变量配置

创建 `.env` 文件：

```bash
# 环境标识
ENVIRONMENT=local  # local/development/staging/production
DEBUG=True

# AWS 配置
AWS_REGION=ap-northeast-1
AWS_PROFILE=your-profile  # 仅本地使用

# Bedrock 配置
BEDROCK_MODEL_ID=us.anthropic.claude-sonnet-4-20250514-v1:0
BEDROCK_REGION=us-west-2
BEDROCK_CROSS_ACCOUNT_ROLE_ARN=arn:aws:iam::905418431228:role/...

# Memory 配置
MEMORY_RESOURCE_ID=CostQ_Dev-Su0pSXBOca  # Dev 环境

# 数据库配置
RDS_SECRET_NAME=costq/rds/postgresql-dev  # Dev 密钥

# 加密密钥
ENCRYPTION_KEY=your-fernet-key

# Gateway MCP 配置
COSTQ_AWS_MCP_SERVERS_GATEWAY_URL=https://xxx.gateway.bedrock-agentcore.ap-northeast-1.amazonaws.com/mcp
```

### 本地运行

```bash
# 直接运行 Runtime
python -m costq_agents.agent.runtime

# 或使用 AgentCore 本地测试工具
bedrock-agentcore local invoke --payload '{"prompt": "查询本月成本"}'
```

## 📁 项目结构

```
costq-agents/
├── costq_agents/           # 主代码包
│   ├── agent/             # Agent 核心代码
│   │   ├── runtime.py     # AgentCore Runtime 入口
│   │   ├── manager.py     # Agent 管理器
│   │   └── prompts/       # 模块化提示词
│   ├── config/            # 配置管理
│   │   ├── settings.py    # 统一配置
│   │   └── aws_secrets.py # Secrets Manager
│   ├── database/          # 数据库模块
│   │   ├── connection.py  # 连接管理
│   │   └── models/        # 数据模型
│   ├── services/          # 服务层
│   │   ├── credential_manager.py        # AWS 凭证管理
│   │   ├── gcp_credential_manager.py    # GCP 凭证管理
│   │   ├── iam_role_session_factory.py  # IAM Role 管理
│   │   └── streamable_http_sigv4.py     # Gateway MCP 认证
│   ├── mcp/               # MCP 模块
│   │   ├── mcp_manager.py          # MCP 客户端管理
│   │   ├── connection_pool.py      # Gateway 连接池
│   │   ├── common_tools_mcp_server/ # 通用工具 MCP
│   │   ├── alert_mcp_server/       # 告警管理 MCP
│   │   ├── send_email_mcp_server/  # 邮件发送 MCP
│   │   └── gcp_cost_mcp_server/    # GCP 成本 MCP
│   └── utils/             # 工具模块
├── config_runtime/        # MCP 配置文件
│   └── mcp_config.json    # MCP Server 配置
├── docs/                  # 文档
├── tests/                 # 测试文件
├── pyproject.toml         # 项目配置
├── requirements.txt       # 依赖管理
├── README.md             # 项目说明（本文件）
├── CODING_STANDARDS.md   # 编码规范
└── DEEPV.md              # 执行规范
```

## 🔧 核心功能

### 1. Agent Runtime

AgentCore Runtime 入口，处理来自 Backend 的查询请求：

- 解析 Payload（prompt, account_id, session_id）
- 查询 RDS 数据库获取账号信息
- AssumeRole 或解密 AKSK 获取临时凭证
- 创建 MCP 客户端（本地 stdio + Gateway HTTP）
- 创建 Agent（BedrockModel + Tools + Memory）
- 流式执行并返回结果

### 2. MCP Manager

管理 MCP 客户端的创建和生命周期：

- **本地 MCP (stdio)**: common-tools, alert, send-email, gcp-cost
- **官方 MCP (PyPI)**: pricing, documentation
- **Gateway MCP (HTTP + SigV4)**: billing-cost-management, risp

### 3. 凭证管理

- **IAM Role 模式**: 自动 AssumeRole 并刷新凭证（推荐）
- **AKSK 模式**: Fernet 加密存储，运行时解密
- **GCP Service Account**: JSON 文件加密存储
- **环境变量隔离**: 使用 `additional_env` 传递凭证，避免污染主进程

### 4. Prompt 系统

模块化提示词管理：

- `core/` - 核心系统约束
- `aws/` - AWS 对话提示词
- `gcp/` - GCP 对话提示词
- `shared/` - 共享片段
- `examples/` - Few-shot 示例
- `alert_agent/` - 告警场景提示词

## 🚢 部署

### Docker 构建

```bash
# 构建镜像
docker build -t costq-agents:latest .

# 推送到 ECR
aws ecr get-login-password --region ap-northeast-1 | docker login --username AWS --password-stdin 000451883532.dkr.ecr.ap-northeast-1.amazonaws.com
docker tag costq-agents:latest 000451883532.dkr.ecr.ap-northeast-1.amazonaws.com/costq-agents:latest
docker push 000451883532.dkr.ecr.ap-northeast-1.amazonaws.com/costq-agents:latest
```

### AgentCore Runtime 部署

```bash
# 创建或更新 Runtime
aws bedrock-agentcore-control create-runtime \
  --name costq-agent-runtime \
  --image-uri 000451883532.dkr.ecr.ap-northeast-1.amazonaws.com/costq-agents:latest \
  --region ap-northeast-1

# 调用 Runtime
aws bedrock-agentcore invoke-runtime \
  --runtime-arn arn:aws:bedrock-agentcore:... \
  --payload '{"prompt": "查询本月成本", "account_id": "aws-account-uuid"}'
```

## 🧪 测试

```bash
# 运行语法检查
python3 -m py_compile costq_agents/agent/runtime.py

# 运行单元测试（待补充）
pytest tests/

# 运行集成测试（待补充）
pytest tests/integration/
```

## 📚 相关文档

- [需求分析](docs/20260123_Agent代码重构/01-需求分析.md) - 重构背景和设计
- [代码文件清单](docs/20260123_Agent代码重构/02-代码文件清单.md) - 迁移文件清单
- [代码Review报告](docs/20260123_Agent代码重构/03-代码Review报告.md) - 代码审查结果
- [编码规范](CODING_STANDARDS.md) - Python 编码标准
- [执行规范](DEEPV.md) - 开发流程规范

## 🔗 相关仓库

- **Backend**: `strands-agent-demo` (单体仓库) - FastAPI Backend + Frontend
- **Gateway MCP**: 独立部署的 MCP Server
  - `billing-cost-management` - 成本优化和管理
  - `risp` - RI/SP 分析

## 📝 版本历史

- **v0.1.0** (2026-01-23) - 初始版本，从单体仓库拆分

## 🤝 贡献指南

1. 遵循 [CODING_STANDARDS.md](CODING_STANDARDS.md) 编码规范
2. 遵循 [DEEPV.md](DEEPV.md) 执行规范
3. 提交前运行代码检查和测试
4. 提交信息使用中文，格式清晰

## 📄 许可证

内部项目，不对外开源。

## 🔒 安全说明

- 凭证使用 Fernet 加密存储
- 数据库密码从 Secrets Manager 读取
- 环境变量隔离，避免凭证泄漏
- 审计日志记录所有操作

## 📞 联系方式

如有问题请联系 CostQ 开发团队。
