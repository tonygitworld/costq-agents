"""
AgentCore Runtime 入口文件

将 CostQ Agent 部署到 AWS AgentCore Runtime。
使用 bedrock-agentcore SDK 的标准模式，直接返回结果。

架构:
    Client (boto3)
      → AgentCore Runtime (本文件)
        → Strands Agent
          → MCP Servers (STDio 子进程)

关键设计:
- ✅ 符合 AgentCore 官方标准（return 而不是 yield）
- ✅ 直接调用 Strands Agent（不使用 StreamingAgentWrapper）
- ✅ MCP 服务器作为子进程启动（STDio 通信）
- ✅ 简单直接（~100 行核心代码）
"""

# ========== 标准库导入 ==========
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

# ========== 第三方库导入 ==========
from bedrock_agentcore import BedrockAgentCoreApp
from opentelemetry import baggage, context, trace

# ========== 类型检查导入 ==========
if TYPE_CHECKING:
    from strands import Agent

# ========== 本地模块导入 ==========
# 添加项目根目录到 Python 路径
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from costq_agents.agent.manager import AgentManager
from costq_agents.mcp.mcp_manager import MCPManager

# ========== 全局变量初始化 ==========
logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)

RUNTIME_START_FILE = "/tmp/.runtime_start_time"


# ========== Runtime 启动时间追踪 ==========
def get_runtime_start_time():
    """获取或创建Runtime启动时间（持久化到文件，容器级别）"""
    if os.path.exists(RUNTIME_START_FILE):
        try:
            with open(RUNTIME_START_FILE) as f:
                return float(f.read())
        except Exception:
            pass
    start_time = time.time()
    try:
        with open(RUNTIME_START_FILE, "w") as f:
            f.write(str(start_time))
    except Exception:
        pass
    return start_time


RUNTIME_START_TIME = get_runtime_start_time()


def get_runtime_uptime():
    """获取Runtime运行时长（秒）"""
    return time.time() - RUNTIME_START_TIME


def is_cold_start(threshold_seconds=60):
    """判断是否冷启动（启动后60秒内认为是冷启动）"""
    return get_runtime_uptime() < threshold_seconds


# ========== Memory 客户端（全局单例）==========
_memory_client = None
_memory_id = None


def _get_or_create_memory_client():
    """获取或创建 MemoryClient（延迟初始化）

    Returns:
        Tuple[Optional[MemoryClient], Optional[str]]: (memory_client, memory_id)
            - memory_client: AgentCore Memory Client 实例
            - memory_id: Memory Resource ID
    """
    global _memory_client, _memory_id
    if _memory_client is not None:
        return (_memory_client, _memory_id)
    try:
        from bedrock_agentcore.memory import MemoryClient

        from costq_agents.config.settings import settings

        memory_region = settings.AWS_REGION
        _memory_client = MemoryClient(region_name=memory_region)
        _memory_id = settings.MEMORY_RESOURCE_ID
        if not _memory_id:
            logger.warning("⚠️ MEMORY_RESOURCE_ID 未配置，Memory 功能将被禁用")
            _memory_client = None
            return (None, None)
        logger.info(
            f"✅ AgentCore Memory Client 初始化成功 - "
            f"Region: {memory_region}, Memory ID: {_memory_id}"
        )
        return (_memory_client, _memory_id)
    except ImportError as e:
        logger.warning(f"⚠️ bedrock-agentcore SDK 未安装: {e}")
        _memory_client = None
        _memory_id = None
        return (None, None)
    except Exception as e:
        logger.error(f"❌ Memory Client 初始化失败: {e}")
        _memory_client = None
        _memory_id = None
        return (None, None)


_get_or_create_memory_client()
app = BedrockAgentCoreApp(debug=True)
mcp_manager = None
agent_manager = None

# ========== Agent/MCP 缓存（跨 invocation 复用）==========
# 利用 microVM 进程持久性，缓存 Agent 和 MCP 连接以消除重复初始化开销
_agent: "Agent | None" = None  # 缓存的 Strands Agent 实例
_clients_dict: dict[str, Any] | None = None  # 缓存的 MCP 客户端字典
_tools: list | None = None  # 缓存的工具列表
_current_model_id: str | None = None  # 当前缓存 Agent 对应的 model_id
_current_prompt_type: str | None = None  # 当前缓存 Agent 对应的 prompt_type
_current_account_type: str | None = None  # 当前缓存 MCP/Agent 对应的 account_type


def _clear_all_cache(reason: str) -> None:
    """清空所有全局缓存变量（异常降级时调用）

    Args:
        reason: 清空原因，用于结构化日志
    """
    global _agent, _clients_dict, _tools
    global _current_model_id, _current_prompt_type, _current_account_type
    logger.warning(
        "🧹 清空全局缓存",
        extra={
            "reason": reason,
            "cleared_vars": [
                "_agent",
                "_clients_dict",
                "_tools",
                "_current_model_id",
                "_current_prompt_type",
                "_current_account_type",
            ],
        },
    )
    _agent = None
    _clients_dict = None
    _tools = None
    _current_model_id = None
    _current_prompt_type = None
    _current_account_type = None


def _clear_mcp_cache(reason: str) -> None:
    """清空 MCP 相关缓存变量

    Args:
        reason: 清空原因，用于结构化日志
    """
    global _clients_dict, _tools, _current_account_type
    logger.warning(
        "🧹 清空 MCP 缓存",
        extra={
            "reason": reason,
            "cleared_vars": [
                "_clients_dict",
                "_tools",
                "_current_account_type",
            ],
        },
    )
    _clients_dict = None
    _tools = None
    _current_account_type = None


def get_or_create_managers():
    """获取或创建全局管理器

    只在第一次调用时创建，后续复用。
    这样可以避免每次请求都重新创建 BedrockModel 和 MCP 连接。

    Returns:
        Tuple: (mcp_manager, agent_manager, dialog_system_prompt, alert_system_prompt)
    """
    global mcp_manager, agent_manager
    if mcp_manager is None:
        logger.info("创建 MCPManager...")
        mcp_manager = MCPManager()
    if agent_manager is None:
        logger.info("创建 AgentManager...")
        from costq_agents.config.settings import settings

        dialog_system_prompt = AgentManager.load_bedrock_prompt(settings.DIALOG_AWS_PROMPT_ARN)
        logger.info(f"✅ 对话提示词加载完成 - 长度: {len(dialog_system_prompt)} 字符")
        alert_system_prompt = AgentManager.load_bedrock_prompt(settings.ALERT_PROMPT_ARN)
        logger.info(f"✅ 告警提示词加载完成 - 长度: {len(alert_system_prompt)} 字符")
        agent_manager = AgentManager(
            system_prompt=dialog_system_prompt, model_id=settings.BEDROCK_MODEL_ID
        )
        logger.info("✅ 默认 AgentManager 已创建（对话场景）")
    from costq_agents.config.settings import settings

    dialog_system_prompt = AgentManager.load_bedrock_prompt(settings.DIALOG_AWS_PROMPT_ARN)
    alert_system_prompt = AgentManager.load_bedrock_prompt(settings.ALERT_PROMPT_ARN)
    return (mcp_manager, agent_manager, dialog_system_prompt, alert_system_prompt)


def log_tool_call(tool_name: str, tool_id: str, tool_input: dict):
    """记录工具调用的详细信息

    这些日志会被 OpenTelemetry 采集并发送到 CloudWatch
    使用 logger.info() 的 extra 参数传递结构化数据
    """
    import json

    logger.info(
        f"🔧 TOOL CALL START - {tool_name}",
        extra={
            "tool_name": tool_name,
            "tool_id": tool_id,
            "tool_input": json.dumps(tool_input, ensure_ascii=False),
            "event_type": "tool_call_start",
        },
    )


def log_tool_result(tool_id: str, tool_result: dict, status: str = "success"):
    """记录工具执行结果

    使用 logger.info() 的 extra 参数传递结构化数据
    """
    import json

    result_preview = json.dumps(tool_result, ensure_ascii=False)[:500]
    logger.info(
        f"✅ TOOL RESULT - {status}",
        extra={
            "tool_id": tool_id,
            "tool_result": result_preview,
            "status": status,
            "event_type": "tool_result",
        },
    )


def filter_event(event: dict) -> dict:
    """
    过滤SSE事件冗余字段，避免触发100MB Runtime限制

    策略：黑名单过滤 - 只移除已知的冗余大字段，保留所有其他字段

    根因：
    - AWS Bedrock AgentCore Runtime 限制：Maximum payload size = 100 MB
    - Strands Agent 每个token事件携带完整上下文（~100KB）
    - 导致约1049个事件后触发限制

    解决：
    - 移除冗余字段（agent、request_state等，~99KB）
    - 保留必需字段（event、delta、data等）
    - 事件大小从 ~100KB 减少到 ~500字节（99.5%减少）
    - 支持事件数从 1,049 增加到 ~200,000（190倍提升）

    优势：
    - 安全：不会意外过滤掉未知但重要的小字段
    - 简单：只需维护"移除列表"
    - 鲁棒：兼容未来可能新增的字段

    Args:
        event: 原始Strands事件

    Returns:
        过滤后的事件（移除了冗余大字段）
    """
    remove_fields = {
        "agent",
        "request_state",
        "event_loop_cycle_trace",
        "event_loop_cycle_span",
        "model",
        "messages",
        "system_prompt",
        "tool_config",
        "event_loop_cycle_id",
    }
    filtered = {k: v for k, v in event.items() if k not in remove_fields}
    if not hasattr(filter_event, "_logged_stats"):
        import json

        try:
            original_size = len(json.dumps(event, ensure_ascii=False, default=str))
            filtered_size = len(json.dumps(filtered, ensure_ascii=False, default=str))
            reduction_ratio = (1 - filtered_size / original_size) * 100 if original_size > 0 else 0
            opt_ratio = round(original_size / filtered_size, 1) if filtered_size else 0
            logger.info(
                f"✅ Event filtering initialized - original: {original_size}B, "
                f"filtered: {filtered_size}B, reduction: {round(reduction_ratio, 2)}%, "
                f"optimization: {opt_ratio}x"
            )
            filter_event._logged_stats = True
        except Exception as e:
            logger.warning(f"⚠️ Failed to log filter stats: {e}")
    return filtered


# ========== 文档附件辅助函数 ==========


def _mime_to_document_format(mime_type: str) -> str:
    """将 MIME 类型映射为 Bedrock Converse API document format

    Bedrock 允许的 format 枚举值: docx, csv, html, txt, pdf, md, doc, xlsx, xls

    Raises:
        ValueError: 当 mime_type 不在白名单内时抛出，由调用方决定是否跳过该文件
    """
    mapping = {
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
        "application/vnd.ms-excel": "xls",
        "application/pdf": "pdf",
        "text/csv": "csv",
        "text/html": "html",
        "application/msword": "doc",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
        "text/markdown": "md",
        "text/plain": "txt",
    }
    result = mapping.get(mime_type)
    if result is None:
        raise ValueError(
            f"不支持的文档 MIME 类型: {mime_type}，Bedrock 支持: {list(mapping.values())}"
        )
    return result


def _sanitize_document_name(file_name: str) -> str:
    """清洗文件名以符合 Bedrock API 要求：仅 [a-zA-Z0-9_-]，最长 200 字符"""
    import re

    name = file_name.rsplit(".", 1)[0] if "." in file_name else file_name
    name = name.replace(" ", "_")
    name = re.sub(r"[^a-zA-Z0-9_-]", "", name)
    if not name:
        name = "document"
    return name[:200]


@app.entrypoint
async def invoke(payload: dict[str, Any]):
    """
    AgentCore Runtime 入口函数（流式输出版本）

    使用官方推荐的 async + stream_async + yield 模式：
    1. 接收 payload
    2. 从数据库查询账号信息
    3. Runtime 内部执行两个 AssumeRole
    4. 根据 prompt_type 选择对应的提示词
    5. 创建 Agent（告警场景不使用 Memory）
    6. 流式执行 Agent（yield 每个事件）

    Args:
        payload: 调用参数
            - prompt: 用户查询（必需）
            - account_id: AWS 账号 ID（必需）
            - account_type: 账号类型（默认: aws，可选: gcp）
            - prompt_type: 提示词类型（默认: "dialog"）
                * "dialog": 使用对话提示词，启用 Memory
                * "alert": 使用告警提示词，禁用 Memory
            - session_id: 会话 ID（可选，对话场景使用）
            - user_id: 用户 ID（可选，对话场景使用）
            - org_id: 组织 ID（可选，对话场景使用）
            - model_id: AI 模型 ID（可选，如不提供则使用默认模型）

    Yields:
        Dict[str, Any]: 流式事件
            - 工具调用事件
            - 文本生成事件
            - 最终结果事件

    Examples:
        >>> # 对话场景（默认）
        >>> payload = {
        ...     "prompt": "查询成本",
        ...     "account_id": "123456789012",
        ...     "session_id": "sess-123",
        ...     "user_id": "user-456",
        ... }
        >>> # prompt_type 默认为 "dialog"，可省略

        >>> # 告警场景
        >>> payload = {
        ...     "prompt": "当日 EC2 成本超过 $1000",
        ...     "account_id": "123456789012",
        ...     "prompt_type": "alert",  # ✅ 关键参数
        ... }
        >>> # 告警场景不需要 session_id、user_id

        >>> # 使用自定义模型
        >>> payload = {
        ...     "prompt": "查询成本",
        ...     "account_id": "123456789012",
        ...     "model_id": "us.anthropic.claude-3-5-haiku-20241022-v1:0",
        ... }
    """
    import json

    from costq_agents.config.settings import settings

    invoke_start_time = time.time()
    runtime_uptime = get_runtime_uptime()
    is_cold = is_cold_start(threshold_seconds=60)
    logger.info(
        "🚀 AgentCore Runtime invocation started ...",
        extra={
            "payload_keys": list(payload.keys()),
            "runtime_uptime_seconds": round(runtime_uptime, 2),
            "is_cold_start": is_cold,
        },
    )
    with tracer.start_as_current_span("costq_agents.agent.invocation") as root_span:
        rds_secret_name = os.getenv("RDS_SECRET_NAME")
        if not rds_secret_name:
            error_msg = "Missing required environment variable: RDS_SECRET_NAME"
            logger.error(error_msg)
            root_span.set_status(trace.Status(trace.StatusCode.ERROR, error_msg))
            yield {"error": error_msg}
            return
        logger.info(
            "Database secret loaded from environment", extra={"secret_name": rds_secret_name}
        )
        encryption_key = os.getenv("ENCRYPTION_KEY")
        if not encryption_key:
            error_msg = "Missing required environment variable: ENCRYPTION_KEY"
            logger.error(error_msg)
            root_span.set_status(trace.Status(trace.StatusCode.ERROR, error_msg))
            yield {"error": error_msg}
            return
        logger.info("Encryption key loaded from environment")
        step1_start = time.time()
        user_message = payload.get("prompt")
        if not user_message:
            error_msg = "Missing required parameter: prompt"
            logger.error(error_msg, extra={"payload": payload})
            root_span.set_status(trace.Status(trace.StatusCode.ERROR, error_msg))
            yield {"error": error_msg}
            return
        account_id = payload.get("account_id")
        if not account_id:
            error_msg = "Missing required parameter: account_id"
            logger.error(error_msg, extra={"payload": payload})
            root_span.set_status(trace.Status(trace.StatusCode.ERROR, error_msg))
            yield {"error": error_msg}
            return
        prompt_type = payload.get("prompt_type", "dialog")
        if prompt_type not in ["dialog", "alert"]:
            error_msg = f"Invalid prompt_type: {prompt_type}. Must be 'dialog' or 'alert'"
            logger.error(error_msg, extra={"payload": payload})
            root_span.set_status(trace.Status(trace.StatusCode.ERROR, error_msg))
            yield {"error": error_msg}
            return
        logger.info("Prompt type determined", extra={"prompt_type": prompt_type})
        root_span.set_attribute("prompt.type", prompt_type)
        account_type = payload.get("account_type", "aws")
        session_id = payload.get("session_id")
        user_id = payload.get("user_id")
        org_id = payload.get("org_id")

        # ✅ 从 payload 提取 model_id，如不提供则使用默认模型
        model_id = payload.get("model_id") or settings.BEDROCK_MODEL_ID
        logger.info("Model ID determined", extra={"model_id": model_id})

        step1_duration = time.time() - step1_start
        logger.debug(
            "⏱️ Step 1: Payload解析完成",
            extra={
                "duration_seconds": round(step1_duration, 3),
                "prompt_type": prompt_type,
                "has_session_id": session_id is not None,
            },
        )
        original_session_id = session_id
        session_renewed = False
        if session_id and prompt_type == "dialog":
            session_check_start = time.time()
            SESSION_MAX_AGE = 7 * 3600
            db = None
            try:
                from sqlalchemy import text

                from costq_agents.database.connection import get_db

                db = next(get_db())
                sql = text(
                    "\n                    SELECT\n                        EXTRACT(EPOCH FROM (NOW() - created_at)) as age_seconds\n                    FROM chat_sessions\n                    WHERE id = :session_id\n                "
                )
                result = db.execute(sql, {"session_id": session_id}).fetchone()
                if result:
                    session_age = result[0]
                    if session_age > SESSION_MAX_AGE:
                        import uuid

                        new_session_id = str(uuid.uuid4())
                        logger.warning(
                            "Session接近过期，创建新session",
                            extra={
                                "old_session_id": str(session_id),
                                "new_session_id": new_session_id,
                                "session_age_hours": session_age / 3600,
                                "max_age_hours": SESSION_MAX_AGE / 3600,
                            },
                        )
                        session_id = new_session_id
                        session_renewed = True
                else:
                    import uuid

                    new_session_id = str(uuid.uuid4())
                    logger.warning(
                        "Session不存在，创建新session",
                        extra={"old_session_id": str(session_id), "new_session_id": new_session_id},
                    )
                    session_id = new_session_id
                    session_renewed = True
            except Exception as e:
                session_check_duration = time.time() - session_check_start
                logger.warning(
                    "⏱️ Session过期检测失败，继续使用原session",
                    extra={
                        "error": str(e),
                        "session_id": str(session_id),
                        "duration_seconds": round(session_check_duration, 3),
                    },
                )
            finally:
                if "session_check_start" in locals():
                    session_check_duration = time.time() - session_check_start
                    logger.debug(
                        "⏱️ Session检测完成",
                        extra={
                            "duration_seconds": round(session_check_duration, 3),
                            "session_renewed": session_renewed,
                        },
                    )
                if db is not None:
                    db.close()
                    logger.debug("数据库连接已关闭")
        root_span.set_attribute("session.id", session_id or "")
        root_span.set_attribute("session.renewed", session_renewed)
        root_span.set_attribute("user.id", user_id or "")
        root_span.set_attribute("account.id", account_id)
        root_span.set_attribute("account.type", account_type)
        root_span.set_attribute("prompt.length", len(user_message))
        logger.info(
            "Parameters validated",
            extra={
                "prompt_length": len(user_message),
                "account_id": account_id,
                "account_type": account_type,
                "has_session_id": session_id is not None,
                "has_user_id": user_id is not None,
                "session_renewed": session_renewed,
            },
        )
        if session_renewed:
            yield {
                "type": "session_renewed",
                "old_session_id": original_session_id,
                "new_session_id": session_id,
                "reason": "session_expired",
                "message": "会话已过期，已自动创建新会话",
            }
        context_token = None
        if session_id:
            ctx = baggage.set_baggage("session.id", session_id)
            if user_id:
                ctx = baggage.set_baggage("user.id", user_id, context=ctx)
            context_token = context.attach(ctx)
            logger.info(
                "🔗 Session context set",
                extra={
                    "session_id": str(session_id),
                    "user_id": str(user_id),
                    "event_type": "session_context",
                },
            )
        db_query_start = time.time()
        logger.info("⏱️ Step 2: 数据库查询开始", extra={"account_id": account_id})
        db = None
        with tracer.start_as_current_span("costq_agents.database.query_account") as db_span:
            db_span.set_attribute("db.operation", "SELECT")
            db_span.set_attribute("account.type", account_type)
            db_span.set_attribute("account.id", account_id)
            try:
                from costq_agents.database.connection import get_db

                logger.info("Database modules imported successfully")
                db = next(get_db())
                logger.info("Database session created")
                from sqlalchemy import text

                if account_type == "gcp":
                    db_span.set_attribute("db.table", "gcp_accounts")
                    sql = text(
                        """
                        SELECT id, project_id, account_name, credentials_encrypted, org_id
                        FROM gcp_accounts
                        WHERE id = :account_id
                        ORDER BY created_at DESC
                        LIMIT 1
                    """
                    )
                    logger.debug(
                        "📊 Using GCP query",
                        extra={"table": "gcp_accounts", "account_id": account_id},
                    )
                else:
                    db_span.set_attribute("db.table", "aws_accounts")
                    sql = text(
                        """
                        SELECT id, account_id, role_arn, org_id, region, auth_type,
                               access_key_id, secret_access_key_encrypted
                        FROM aws_accounts
                        WHERE id = :account_id
                        ORDER BY created_at DESC
                        LIMIT 1
                    """
                    )
                    logger.debug(
                        "📊 Using AWS query",
                        extra={"table": "aws_accounts", "account_id": account_id},
                    )
                logger.info(
                    "Executing database query",
                    extra={"account_id": account_id, "account_type": account_type},
                )
                sql_exec_start = time.time()
                result = db.execute(sql, {"account_id": account_id}).fetchone()
                sql_exec_duration = time.time() - sql_exec_start
                logger.debug(
                    "⏱️ SQL执行完成",
                    extra={
                        "duration_seconds": round(sql_exec_duration, 3),
                        "result_found": bool(result),
                    },
                )
                if not result:
                    error_msg = f"Account not found: {account_id} (type: {account_type})"
                    logger.error(
                        error_msg,
                        extra={
                            "account_id": account_id,
                            "account_type": account_type,
                            "sql_query": str(sql),
                            "table": "gcp_accounts" if account_type == "gcp" else "aws_accounts",
                        },
                    )
                    db_span.set_status(trace.Status(trace.StatusCode.ERROR, error_msg))
                    db_span.set_attribute("account.found", False)
                    root_span.set_status(trace.Status(trace.StatusCode.ERROR, error_msg))
                    yield {"error": error_msg}
                    return
                db_span.set_attribute("account.found", True)
                if account_type == "gcp":
                    account_uuid = result[0]
                    project_id = result[1]
                    account_name = result[2]
                    credentials_encrypted = result[3]
                    org_id = result[4]
                    account_id_db = project_id
                    role_arn = None
                    region = None
                    auth_type = "service_account"
                    access_key_id = None
                    secret_key_encrypted = credentials_encrypted
                    db_span.set_attribute("gcp.project_id", project_id)
                    db_span.set_attribute("gcp.account_name", account_name)
                else:
                    account_uuid = result[0]
                    account_id_db = result[1]
                    role_arn = result[2]
                    org_id = result[3]
                    region = result[4] or "us-east-1"
                    auth_type = result[5] or "aksk"
                    access_key_id = result[6]
                    secret_key_encrypted = result[7]
                    db_span.set_attribute("auth.type", auth_type)
                    db_span.set_attribute("account.region", region)
                db_query_duration = time.time() - db_query_start
                logger.info(
                    "⏱️ Account info retrieved successfully",
                    extra={
                        "account_uuid": str(account_uuid),
                        "account_id": account_id_db,
                        "auth_type": auth_type,
                        "org_id": str(org_id),
                        "region": region,
                        "db_query_duration_seconds": round(db_query_duration, 3),
                    },
                )
            except ValueError as e:
                error_msg = f"Invalid account_id parameter: {str(e)}"
                logger.error(
                    error_msg, extra={"account_id": account_id, "error_type": "ValidationError"}
                )
                db_span.set_status(trace.Status(trace.StatusCode.ERROR, error_msg))
                root_span.set_status(trace.Status(trace.StatusCode.ERROR, error_msg))
                yield {"error": error_msg, "error_type": "client_error"}
                return
            except ImportError as e:
                error_msg = f"Database module import failed: {str(e)}"
                logger.error(error_msg, extra={"error_type": "ConfigurationError"})
                import traceback

                logger.error("Import error traceback", extra={"traceback": traceback.format_exc()})
                db_span.set_status(trace.Status(trace.StatusCode.ERROR, error_msg))
                root_span.set_status(trace.Status(trace.StatusCode.ERROR, error_msg))
                yield {"error": error_msg, "error_type": "server_error"}
                return
            except Exception as e:
                error_msg = f"Database query failed: {str(e)}"
                logger.error(
                    error_msg,
                    extra={
                        "account_id": account_id,
                        "error_type": type(e).__name__,
                        "error_details": str(e),
                    },
                )
                import traceback

                logger.error(
                    "Database query traceback", extra={"traceback": traceback.format_exc()}
                )
                db_span.set_status(trace.Status(trace.StatusCode.ERROR, error_msg))
                root_span.set_status(trace.Status(trace.StatusCode.ERROR, error_msg))
                yield {"error": error_msg, "error_type": "database_error"}
                return
            finally:
                if db is not None:
                    db.close()
                    logger.info("Database session closed")
    logger.info("Step 3: Creating managers (before setting env vars)")
    try:
        mcp_mgr, agent_mgr, dialog_system_prompt, alert_system_prompt = get_or_create_managers()
        logger.info(
            "Managers created successfully",
            extra={
                "has_mcp_manager": mcp_mgr is not None,
                "has_agent_manager": agent_mgr is not None,
                "dialog_prompt_len": len(dialog_system_prompt),
                "alert_prompt_len": len(alert_system_prompt),
            },
        )
    except ImportError as e:
        error_msg = f"Failed to import manager modules: {str(e)}"
        logger.error(error_msg, extra={"error_type": "ImportError", "error_details": str(e)})
        import traceback

        logger.error("Manager import traceback", extra={"traceback": traceback.format_exc()})
        yield {"error": error_msg, "error_type": "server_error"}
        return
    except ValueError as e:
        error_msg = f"Invalid manager configuration: {str(e)}"
        logger.error(error_msg, extra={"error_type": "ConfigurationError"})
        yield {"error": error_msg, "error_type": "configuration_error"}
        return
    except Exception as e:
        error_msg = f"Failed to create managers: {str(e)}"
        logger.error(error_msg, extra={"error_type": type(e).__name__, "error_details": str(e)})
        import traceback

        logger.error("Manager creation traceback", extra={"traceback": traceback.format_exc()})
        yield {"error": error_msg, "error_type": "internal_error"}
        return
    # ========== ✅ 【关键修改】创建隔离的环境变量字典 ==========
    # 不再使用 os.environ 设置凭证，改为使用 additional_env 字典
    # 这样可以避免污染主进程环境，OpenTelemetry 会继续使用 Runtime IAM Role
    additional_env: dict[str, str] = {}

    # ✅ 用于清理 GCP 临时凭证文件（已废弃，保留占位）
    gcp_temp_file: str | None = None

    credentials_start = time.time()
    logger.info(
        f"Step 4: Getting credentials (auth_type={auth_type}, using env isolation)",
        extra={
            "auth_type": auth_type,
            "org_id": str(org_id),
            "region": region,
            "env_isolation_enabled": True,  # ✅ 标记：使用环境变量隔离
        },
    )
    if auth_type == "iam_role":
        if not role_arn:
            error_msg = f"IAM Role not configured for account: {account_id}"
            logger.error(error_msg, extra={"account_id": account_id})
            yield {"error": error_msg}
            return
        try:
            from costq_agents.services.iam_role_session_factory import IAMRoleSessionFactory

            logger.info("IAMRoleSessionFactory imported")
            external_id = f"org-{org_id}" if org_id else None
            logger.info("Generated external_id", extra={"external_id": external_id})
            logger.info("Creating IAMRoleSessionFactory instance")
            target_factory = IAMRoleSessionFactory.get_instance(
                account_id=account_id, role_arn=role_arn, external_id=external_id, region=region
            )
            logger.info("IAMRoleSessionFactory instance created")
            logger.info("Getting temporary credentials")
            target_credentials = target_factory.get_current_credentials()
            logger.info(
                "IAM Role credentials obtained (storing to isolated env dict)",
                extra={
                    "access_key_prefix": target_credentials["access_key_id"][:20],
                    "has_secret_key": bool(target_credentials.get("secret_access_key")),
                    "has_session_token": bool(target_credentials.get("session_token")),
                    "env_isolation": True,  # ✅ 标记：隔离存储
                },
            )
            # ✅ 存储到隔离字典，不设置 os.environ（避免污染主进程）
            additional_env["AWS_ACCESS_KEY_ID"] = target_credentials["access_key_id"]
            additional_env["AWS_SECRET_ACCESS_KEY"] = target_credentials["secret_access_key"]
            additional_env["AWS_SESSION_TOKEN"] = target_credentials["session_token"]
        except Exception as e:
            error_msg = f"AssumeRole to target account failed: {str(e)}"
            logger.error(
                error_msg,
                extra={
                    "role_arn": role_arn,
                    "account_id": account_id,
                    "error_type": type(e).__name__,
                    "error_details": str(e),
                },
            )
            import traceback

            logger.error("AssumeRole traceback", extra={"traceback": traceback.format_exc()})
            yield {"error": error_msg}
            return
    elif auth_type == "service_account":
        if not secret_key_encrypted:
            error_msg = f"Service account JSON not configured for GCP account: {account_id}"
            logger.error(error_msg, extra={"account_id": account_id})
            yield {"error": error_msg}
            return
        # GCP Gateway 模式：不再在 Runtime 解密并写入临时文件
        logger.info(
            "GCP credentials will be resolved by Gateway runtime (no local temp file)",
            extra={"account_id": account_id, "env_isolation": True},
        )
    else:
        if not access_key_id or not secret_key_encrypted:
            error_msg = f"AKSK credentials not configured for account: {account_id}"
            logger.error(error_msg, extra={"account_id": account_id})
            yield {"error": error_msg}
            return
        try:
            from costq_agents.services.credential_manager import get_credential_manager

            logger.info("Decrypting AKSK credentials")
            credential_manager = get_credential_manager()
            secret_access_key = credential_manager.decrypt_secret_key(secret_key_encrypted)
            logger.info(
                "AKSK credentials decrypted successfully (storing to isolated env dict)",
                extra={
                    "access_key_prefix": access_key_id[:20],
                    "env_isolation": True,  # ✅ 标记：隔离存储
                },
            )
            # ✅ 存储到隔离字典，不设置 os.environ
            additional_env["AWS_ACCESS_KEY_ID"] = access_key_id
            additional_env["AWS_SECRET_ACCESS_KEY"] = secret_access_key
            # ✅ 确保不传递 SESSION_TOKEN（AKSK 不需要）
            # 注意：不需要删除 os.environ 中的，因为我们根本没设置
        except Exception as e:
            error_msg = f"Failed to decrypt AKSK credentials: {str(e)}"
            logger.error(
                error_msg,
                extra={
                    "account_id": account_id,
                    "error_type": type(e).__name__,
                    "error_details": str(e),
                },
            )
            import traceback

            logger.error("AKSK decryption traceback", extra={"traceback": traceback.format_exc()})
            yield {"error": error_msg}
            return
    if account_type == "gcp":
        logger.info("✅ GCP 凭证已准备（隔离字典，不污染主进程）", extra={"env_isolation": True})
    else:
        # ✅ AWS 区域信息也存储到隔离字典
        additional_env["AWS_REGION"] = region
        additional_env["AWS_DEFAULT_REGION"] = region
        is_container = os.environ.get("DOCKER_CONTAINER") == "1"
        if not is_container:
            # 平台 Profile 传递给 MCP（本地开发使用）
            additional_env["PLATFORM_AWS_PROFILE"] = os.environ.get("AWS_PROFILE", "3532")
            logger.info(f"设置平台 Profile（隔离传递）: {additional_env['PLATFORM_AWS_PROFILE']}")
        logger.info(
            f"✅ {auth_type.upper()} 凭证已准备（隔离字典，不污染主进程）",
            extra={
                "auth_type": auth_type,
                "env_isolation": True,
                "env_vars_count": len(additional_env),
            },
        )
    credentials_duration = time.time() - credentials_start
    logger.info(
        "⏱️ Step 3-5: AWS凭证获取完成",
        extra={"auth_type": auth_type, "duration_seconds": round(credentials_duration, 3)},
    )
    # ========== Step 6: MCP 客户端条件创建（缓存复用）==========
    global _clients_dict, _tools, _current_account_type

    need_rebuild_mcp = _clients_dict is None or _current_account_type != account_type

    with tracer.start_as_current_span("costq_agents.mcp.initialize") as mcp_span:
        try:
            mcp_start_time = time.time()
            mcp_span.set_attribute("costq_agents.mcp.account_type", account_type)

            if need_rebuild_mcp:
                # 关闭旧连接（account_type 切换时）
                previous_account_type = _current_account_type
                if _clients_dict is not None:
                    logger.info(
                        "关闭旧 MCP 连接（account_type 切换）",
                        extra={
                            "previous_account_type": previous_account_type,
                            "new_account_type": account_type,
                        },
                    )
                    mcp_mgr.close_all_clients(_clients_dict)

                logger.info("创建 MCP 客户端...")

                if account_type == "gcp":
                    available_mcps = []
                    logger.info("GCP场景：仅使用 Gateway MCP，不加载 Local MCP")
                else:
                    available_mcps = settings.AWS_MCP_SERVERS
                    logger.info(f"AWS场景：{len(available_mcps)}个 Local MCP")

                mcp_span.set_attribute("costq_agents.mcp.servers_requested", len(available_mcps))
                logger.info(
                    "Step 6: Creating MCP clients (with env isolation)",
                    extra={
                        "available_mcps": available_mcps,
                        "mcp_count": len(available_mcps),
                        "env_isolation_enabled": True,
                        "additional_env_count": len(additional_env),
                    },
                )
                # ✅ 传递隔离的环境变量给 MCP Clients
                new_clients_dict = mcp_mgr.create_all_clients(
                    server_types=available_mcps,
                    additional_env=additional_env,
                )
                mcp_elapsed = time.time() - mcp_start_time
                mcp_span.set_attribute("costq_agents.mcp.clients_created", len(new_clients_dict))
                mcp_span.set_attribute("costq_agents.mcp.elapsed_seconds", round(mcp_elapsed, 2))

                # ✅ 验证主进程环境变量没有被污染
                from costq_agents.utils.env_isolation_validator import (
                    verify_env_isolation,
                )

                isolation_ok = verify_env_isolation(phase="after_mcp_creation")
                if not isolation_ok:
                    logger.error(
                        "🚨 严重：环境变量隔离失败！",
                        extra={
                            "phase": "after_mcp_creation",
                            "impact": "OpenTelemetry/Bedrock/Memory 可能使用了错误的凭证",
                        },
                    )

                logger.info(
                    "MCP clients created (env isolation verified)",
                    extra={
                        "success_count": len(new_clients_dict),
                        "requested_count": len(available_mcps),
                        "created_types": list(new_clients_dict.keys()),
                        "elapsed_seconds": round(mcp_elapsed, 2),
                        "env_isolation_verified": isolation_ok,
                    },
                )

                # 收集工具列表
                new_tools: list = []
                tool_details: dict = {}

                # 1. 收集本地 MCP 工具（stdio 模式）
                for server_type, client in new_clients_dict.items():
                    try:
                        logger.info(f"Getting tools from {server_type}")
                        server_tools = client.list_tools_sync()
                        new_tools.extend(server_tools)
                        tool_details[server_type] = len(server_tools)
                        logger.info(
                            f"✅ Tools from {server_type}",
                            extra={"tool_count": len(server_tools)},
                        )
                    except Exception as e:
                        logger.error(
                            f"❌ Failed to load tools from {server_type}",
                            extra={
                                "server_type": server_type,
                                "error_type": type(e).__name__,
                                "error": str(e),
                            },
                        )
                        tool_details[server_type] = 0

                local_tools_count = len(new_tools)
                logger.info(
                    "Local MCP tools loaded",
                    extra={
                        "local_tools_count": local_tools_count,
                        "tools_per_mcp": tool_details,
                    },
                )

                # 2. 收集 Gateway MCP 工具（HTTP + SigV4 模式）
                if account_type == "aws":
                    gateway_url = settings.COSTQ_AWS_MCP_SERVERS_GATEWAY_URL
                    if gateway_url:
                        try:
                            logger.info(
                                "Step 6.1: Creating AWS Gateway MCP client (SigV4)",
                                extra={
                                    "gateway_url": (
                                        gateway_url[:50] + "..."
                                        if len(gateway_url) > 50
                                        else gateway_url
                                    ),
                                },
                            )
                            gateway_client = mcp_mgr.create_gateway_client(name="gateway-mcp")
                            gateway_client.__enter__()
                            gateway_tools = mcp_mgr.get_full_tools_list(gateway_client)
                            new_tools.extend(gateway_tools)
                            tool_details["gateway"] = len(gateway_tools)
                            logger.info(
                                "✅ Gateway MCP tools loaded (dynamically)",
                                extra={
                                    "gateway_tools_count": len(gateway_tools),
                                },
                            )
                            new_clients_dict["gateway"] = gateway_client
                        except Exception as e:
                            logger.error(
                                "❌ Failed to load AWS Gateway MCP tools",
                                extra={
                                    "error_type": type(e).__name__,
                                    "error": str(e),
                                    "gateway_url": (
                                        gateway_url[:50] + "..."
                                        if len(gateway_url) > 50
                                        else gateway_url
                                    ),
                                },
                            )
                            tool_details["gateway"] = 0
                    else:
                        logger.info(
                            "AWS Gateway MCP 未配置"
                            "（COSTQ_AWS_MCP_SERVERS_GATEWAY_URL 未设置），跳过"
                        )
                elif account_type == "gcp":
                    gateway_url = settings.COSTQ_GCP_MCP_SERVERS_GATEWAY_URL
                    if gateway_url:
                        try:
                            logger.info(
                                "Step 6.1: Creating GCP Gateway MCP client (SigV4)",
                                extra={
                                    "gateway_url": (
                                        gateway_url[:50] + "..."
                                        if len(gateway_url) > 50
                                        else gateway_url
                                    ),
                                },
                            )
                            gateway_client = mcp_mgr.create_gcp_gateway_client(
                                name="gcp-gateway-mcp"
                            )
                            gateway_client.__enter__()
                            gateway_tools = mcp_mgr.get_full_tools_list(gateway_client)
                            new_tools.extend(gateway_tools)
                            tool_details["gateway-gcp"] = len(gateway_tools)
                            logger.info(
                                "✅ GCP Gateway MCP tools loaded (dynamically)",
                                extra={
                                    "gateway_tools_count": len(gateway_tools),
                                },
                            )
                            new_clients_dict["gateway-gcp"] = gateway_client
                        except Exception as e:
                            logger.error(
                                "❌ Failed to load GCP Gateway MCP tools",
                                extra={
                                    "error_type": type(e).__name__,
                                    "error": str(e),
                                    "gateway_url": (
                                        gateway_url[:50] + "..."
                                        if len(gateway_url) > 50
                                        else gateway_url
                                    ),
                                },
                            )
                            tool_details["gateway-gcp"] = 0
                    else:
                        logger.info(
                            "GCP Gateway MCP 未配置"
                            "（COSTQ_GCP_MCP_SERVERS_GATEWAY_URL 未设置），跳过"
                        )

                # 更新全局缓存
                _clients_dict = new_clients_dict
                _tools = new_tools
                _current_account_type = account_type

                # 确定重建原因
                if previous_account_type is None:
                    rebuild_reason = "first_invocation"
                else:
                    rebuild_reason = "account_type_changed"

                mcp_span.set_attribute("costq_agents.mcp.total_tools", len(new_tools))
                mcp_span.set_attribute("costq_agents.mcp.local_tools", local_tools_count)
                mcp_span.set_attribute(
                    "costq_agents.mcp.gateway_tools",
                    len(new_tools) - local_tools_count,
                )
                logger.info(
                    "MCP 缓存已更新",
                    extra={
                        "mcp_reused": False,
                        "rebuild_reason": rebuild_reason,
                        "previous_account_type": previous_account_type,
                        "account_type": account_type,
                        "total_tools": len(new_tools),
                        "local_tools": local_tools_count,
                        "gateway_tools": len(new_tools) - local_tools_count,
                        "tools_per_mcp": tool_details,
                    },
                )
            else:
                # ✅ 复用缓存的 MCP 客户端和工具列表
                mcp_elapsed = time.time() - mcp_start_time
                logger.info(
                    "MCP 缓存命中，复用已有连接",
                    extra={
                        "mcp_reused": True,
                        "account_type": account_type,
                        "cached_mcp_count": len(_clients_dict),
                        "cached_tools_count": len(_tools) if _tools else 0,
                        "elapsed_seconds": round(mcp_elapsed, 2),
                    },
                )

            # 使用缓存值（无论是新建还是复用）
            clients_dict = _clients_dict
            tools = _tools

        except Exception as e:
            _clear_mcp_cache(reason=f"mcp_creation_error: {type(e).__name__}: {str(e)}")
            logger.error(f"创建 MCP 客户端失败: {e}")
            import traceback

            logger.error(traceback.format_exc())
            mcp_span.set_status(trace.Status(trace.StatusCode.ERROR, str(e)))
            root_span.set_status(trace.Status(trace.StatusCode.ERROR, str(e)))
            yield {"error": f"Failed to create MCP clients: {str(e)}"}
            return
    # ✅ 不再需要清理环境变量（因为从未污染 os.environ）
    # 查询账号凭证仅在 additional_env 字典中，已随 MCP 子进程传递
    # 主进程环境变量保持干净，OpenTelemetry/Bedrock/Memory 继续使用 Runtime IAM Role
    logger.info(
        "✅ 环境变量隔离成功：主进程未被污染",
        extra={
            "auth_type": auth_type,
            "env_isolation_verified": "AWS_ACCESS_KEY_ID" not in os.environ,
            "benefit": "OpenTelemetry/Bedrock/Memory 继续使用 Runtime IAM Role（不受查询账号影响）",
        },
    )
    # ========== Step 7: Agent 条件创建（缓存复用）==========
    global _agent, _current_model_id, _current_prompt_type

    need_rebuild_agent = (
        _agent is None
        or _current_model_id != model_id
        or _current_prompt_type != prompt_type
        or _current_account_type != account_type
    )

    step7_start_time = time.time()
    try:
        if need_rebuild_agent:
            # 确定重建原因
            if _agent is None:
                agent_rebuild_reason = "first_invocation"
            else:
                changed = []
                if _current_model_id != model_id:
                    changed.append("model_id")
                if _current_prompt_type != prompt_type:
                    changed.append("prompt_type")
                if _current_account_type != account_type:
                    changed.append("account_type")
                agent_rebuild_reason = "params_changed: " + ",".join(changed)
            previous_model_id = _current_model_id
            previous_prompt_type = _current_prompt_type

            logger.info(
                "⏱️ SPAN START: Step 7 - Agent创建（重建）",
                extra={
                    "tool_count": len(tools),
                    "prompt_type": prompt_type,
                    "has_session_id": session_id is not None,
                    "has_org_id": org_id is not None,
                    "rebuild_reason": agent_rebuild_reason,
                },
            )
            memory_client = None
            memory_id = None

            # 使用前端传过来的 model_id 创建 AgentManager
            dialog_agent_manager = AgentManager(
                system_prompt=dialog_system_prompt, model_id=model_id
            )
            logger.info(
                "AgentManager 已创建",
                extra={"model_id": model_id},
            )

            if prompt_type == "dialog" and account_type == "gcp":
                gcp_prompt = AgentManager.load_bedrock_prompt(settings.DIALOG_GCP_PROMPT_ARN)
                logger.info("✅ GCP 对话提示词加载完成" f" - 长度: {len(gcp_prompt)} 字符")
                dialog_agent_manager = AgentManager(system_prompt=gcp_prompt, model_id=model_id)
            if prompt_type == "alert":
                logger.info("创建告警 Agent（使用告警提示词，无 Memory）")
                alert_prompt = AgentManager.load_bedrock_prompt(settings.ALERT_PROMPT_ARN)
                alert_agent_manager = AgentManager(system_prompt=alert_prompt, model_id=model_id)
                agent = alert_agent_manager.create_agent_with_memory(tools=tools)
                logger.info(
                    "Agent 创建完成（告警场景）",
                    extra={
                        "has_memory": False,
                        "tool_count": len(tools),
                    },
                )
            else:
                logger.info("创建对话 Agent（尝试Memory模式）")
                agent_created = False
                memory_fallback_reason = None
                executor = None
                try:
                    memory_init_start = time.time()
                    memory_client, memory_id = _get_or_create_memory_client()
                    memory_init_duration = time.time() - memory_init_start
                    logger.info(
                        "⏱️ Memory客户端初始化完成",
                        extra={
                            "memory_id": memory_id,
                            "duration_seconds": round(memory_init_duration, 2),
                        },
                    )
                    import asyncio
                    from concurrent.futures import ThreadPoolExecutor

                    executor = ThreadPoolExecutor(max_workers=1)
                    agent_create_start = time.time()

                    async def create_with_timeout():
                        loop = asyncio.get_event_loop()
                        return await loop.run_in_executor(
                            executor,
                            dialog_agent_manager.create_agent_with_memory,
                            tools,
                            memory_client,
                            memory_id,
                            user_id,
                            session_id,
                            40,
                        )

                    agent = await asyncio.wait_for(create_with_timeout(), timeout=30.0)
                    if dialog_agent_manager is not agent_mgr:
                        logger.info("✅ 已使用 GCP 对话提示词创建 Agent")
                    agent_create_duration = time.time() - agent_create_start
                    if agent is None:
                        raise ValueError("Agent创建返回None")
                    if not hasattr(agent, "stream_async"):
                        raise ValueError("Agent对象缺少stream_async方法")
                    agent_created = True
                    logger.info(
                        "⏱️ Agent创建完成（对话场景，Memory模式）",
                        extra={
                            "has_memory": True,
                            "session_id": str(session_id),
                            "user_id": str(user_id),
                            "tool_count": len(tools),
                            "duration_seconds": round(agent_create_duration, 2),
                        },
                    )
                except TimeoutError:
                    memory_fallback_reason = "Memory初始化超时（30秒）"
                    logger.warning(
                        memory_fallback_reason,
                        extra={
                            "session_id": str(session_id),
                            "user_id": str(user_id),
                        },
                    )
                except Exception as memory_error:
                    memory_fallback_reason = f"Memory初始化失败: {str(memory_error)}"
                    logger.warning(
                        "Memory初始化失败，回退到无Memory模式",
                        extra={
                            "error_type": type(memory_error).__name__,
                            "error_message": str(memory_error),
                            "session_id": str(session_id),
                        },
                    )
                finally:
                    if executor is not None:
                        executor.shutdown(wait=False)
                        logger.debug("ThreadPoolExecutor已关闭")
                if not agent_created:
                    try:
                        logger.info("使用无Memory模式创建Agent（回退）")
                        agent = dialog_agent_manager.create_agent_with_memory(tools=tools)
                        if agent is None:
                            raise ValueError("Agent创建返回None（无Memory模式）")
                        if not hasattr(agent, "stream_async"):
                            raise ValueError("Agent对象缺少stream_async方法" "（无Memory模式）")
                        logger.info(
                            "Agent 创建完成" "（对话场景，无Memory模式 - 回退）",
                            extra={
                                "has_memory": False,
                                "tool_count": len(tools),
                                "fallback_reason": (memory_fallback_reason),
                            },
                        )
                    except Exception as fallback_error:
                        error_msg = "Agent创建完全失败（包括回退）: " f"{str(fallback_error)}"
                        logger.error(
                            error_msg,
                            extra={
                                "original_error": (memory_fallback_reason),
                                "fallback_error": str(fallback_error),
                            },
                        )
                        raise ValueError(error_msg)

            # 更新 Agent 缓存
            _agent = agent
            _current_model_id = model_id
            _current_prompt_type = prompt_type

            step7_duration = time.time() - step7_start_time
            logger.info(
                "⏱️ SPAN END: Step 7 - Agent创建完成",
                extra={
                    "total_duration_seconds": round(step7_duration, 2),
                    "agent_reused": False,
                    "rebuild_reason": agent_rebuild_reason,
                    "previous_model_id": previous_model_id,
                    "previous_prompt_type": previous_prompt_type,
                    "model_id": model_id,
                    "prompt_type": prompt_type,
                    "account_type": account_type,
                },
            )
        else:
            # ✅ 复用缓存的 Agent 对象
            agent = _agent
            memory_client = None  # Agent 复用时不重新初始化 Memory
            step7_duration = time.time() - step7_start_time
            logger.info(
                "Agent 缓存命中，复用已有 Agent",
                extra={
                    "agent_reused": True,
                    "model_id": model_id,
                    "prompt_type": prompt_type,
                    "account_type": account_type,
                    "elapsed_seconds": round(step7_duration, 2),
                },
            )
    except Exception as e:
        step7_duration = time.time() - step7_start_time
        error_msg = f"Failed to create agent: {str(e)}"
        logger.error(
            "⏱️ SPAN END: Step 7 - Agent创建失败",
            extra={
                "error_msg": error_msg,
                "error_type": type(e).__name__,
                "error_details": str(e),
                "duration_seconds": round(step7_duration, 2),
            },
        )
        import traceback

        logger.error("Agent creation traceback")
        root_span.set_status(trace.Status(trace.StatusCode.ERROR, error_msg))
        yield {"error": error_msg}
        return
    stream_start_time = time.time()
    event_count = 0
    last_event_time = stream_start_time

    # Token 使用统计（流式结束后发送给前端）
    token_usage: dict[str, int | float] = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "input_cache_hit_rate": 0.0,
        "output_cache_hit_rate": 0.0,
    }

    with tracer.start_as_current_span("costq_agents.agent.execute") as exec_span:
        try:
            exec_span.set_attribute("costq_agents.agent.prompt", user_message[:200])
            exec_span.set_attribute(
                "costq_agents.agent.has_memory",
                memory_client is not None and session_id is not None,
            )
            logger.info(
                "⏱️ SPAN START: Step 8 - Agent流式执行",
                extra={
                    "prompt": (
                        user_message[:100] + "..." if len(user_message) > 100 else user_message
                    )
                },
            )
            # ✅ 构建用户消息（支持多模态：文本 + 图片 + 文档）
            images_data = payload.get("images")
            files_data = payload.get("files")
            has_images = images_data and len(images_data) > 0
            has_files = files_data and len(files_data) > 0

            if has_images or has_files:
                import base64

                user_content = [{"text": user_message}]

                # 追加图片 content blocks
                if has_images:
                    for img in images_data:
                        try:
                            mime_type = img.get("mime_type", "image/jpeg")
                            b64_data = img.get("base64_data", "")
                            img_bytes = base64.b64decode(b64_data)
                            user_content.append(
                                {
                                    "image": {
                                        "format": mime_type.split("/")[-1],
                                        "source": {
                                            "bytes": img_bytes,
                                        },
                                    }
                                }
                            )
                        except Exception as e:
                            logger.warning(
                                "⚠️ 图片附件处理失败，跳过该图片",
                                extra={"file_name": img.get("file_name"), "error": str(e)},
                            )

                # 追加文档 content blocks（Excel 等）
                if has_files:
                    for file_item in files_data:
                        try:
                            mime_type = file_item.get("mime_type", "application/octet-stream")
                            doc_format = _mime_to_document_format(mime_type)
                            doc_name = _sanitize_document_name(
                                file_item.get("file_name", "document")
                            )
                            b64_data = file_item.get("base64_data", "")
                            user_content.append(
                                {
                                    "document": {
                                        "format": doc_format,
                                        "name": doc_name,
                                        "source": {
                                            "bytes": base64.b64decode(b64_data),
                                        },
                                    }
                                }
                            )
                        except Exception as e:
                            logger.warning(
                                "⚠️ 文档附件处理失败，跳过该文件",
                                extra={"file_name": file_item.get("file_name"), "error": str(e)},
                            )

                logger.info(
                    "📎 多模态消息构建完成",
                    extra={
                        "text_length": len(user_message),
                        "image_count": len(images_data) if has_images else 0,
                        "file_count": len(files_data) if has_files else 0,
                        "image_types": [img.get("mime_type") for img in images_data]
                        if has_images
                        else [],
                        "file_types": [f.get("mime_type") for f in files_data] if has_files else [],
                    },
                )
                stream = agent.stream_async(user_content)
            else:
                stream = agent.stream_async(user_message)
            logger.info("Agent stream started")
            async for event in stream:
                event_count += 1
                current_time = time.time()
                event_interval = current_time - last_event_time
                if event_interval > 5.0:
                    event_type = "unknown"
                    if isinstance(event, dict) and "event" in event:
                        event_data = event["event"]
                        if "contentBlockStart" in event_data:
                            event_type = "tool_start"
                        elif "contentBlockDelta" in event_data:
                            event_type = "text_delta"
                    logger.warning(
                        "⏱️ 长间隔事件检测",
                        extra={
                            "interval_seconds": round(event_interval, 2),
                            "event_type": event_type,
                            "event_count": event_count,
                            "cumulative_seconds": round(current_time - stream_start_time, 2),
                        },
                    )
                last_event_time = current_time
                if isinstance(event, dict):
                    if "event" in event:
                        event_data = event["event"]
                        if "contentBlockStart" in event_data:
                            start = event_data["contentBlockStart"].get("start", {})
                            if "toolUse" in start:
                                tool_use = start["toolUse"]
                                tool_name = tool_use.get("name")
                                tool_id = tool_use.get("toolUseId")
                                tool_span = tracer.start_span(
                                    f"tool.{tool_name}",
                                    attributes={
                                        "tool.name": tool_name,
                                        "tool.id": tool_id,
                                        "tool.input": json.dumps(tool_use.get("input", {}))[:500],
                                    },
                                )
                                if not hasattr(exec_span, "_tool_spans"):
                                    exec_span._tool_spans = {}
                                exec_span._tool_spans[tool_id] = tool_span
                                log_tool_call(
                                    tool_name=tool_name,
                                    tool_id=tool_id,
                                    tool_input=tool_use.get("input", {}),
                                )
                    if "message" in event:
                        message = event["message"]
                        if message.get("role") == "user":
                            for content in message.get("content", []):
                                if isinstance(content, dict) and "toolResult" in content:
                                    tool_result = content["toolResult"]
                                    tool_id = tool_result.get("toolUseId")
                                    result_data = {}
                                    for item in tool_result.get("content", []):
                                        if isinstance(item, dict):
                                            if "json" in item:
                                                try:
                                                    result_data = (
                                                        item["json"]
                                                        if isinstance(item["json"], dict)
                                                        else json.loads(item["json"])
                                                    )
                                                except (
                                                    json.JSONDecodeError,
                                                    TypeError,
                                                    ValueError,
                                                ):
                                                    result_data = {"raw": str(item["json"])}
                                            elif "text" in item:
                                                result_data = {"text": item["text"]}
                                    if (
                                        hasattr(exec_span, "_tool_spans")
                                        and tool_id in exec_span._tool_spans
                                    ):
                                        tool_span = exec_span._tool_spans[tool_id]
                                        tool_span.set_attribute(
                                            "tool.status", tool_result.get("status", "success")
                                        )
                                        tool_span.set_attribute(
                                            "tool.result", json.dumps(result_data)[:500]
                                        )
                                        tool_span.end()
                                        del exec_span._tool_spans[tool_id]
                                    log_tool_result(
                                        tool_id=tool_id,
                                        tool_result=result_data,
                                        status=tool_result.get("status", "success"),
                                    )

                    # 提取 Token 使用统计（从 result 事件的 metrics.accumulated_usage 获取）
                    if "result" in event:
                        try:
                            result = event["result"]
                            if hasattr(result, "metrics") and result.metrics:
                                metrics = result.metrics
                                if hasattr(metrics, "accumulated_usage"):
                                    usage_data = metrics.accumulated_usage
                                    # Strands SDK 使用驼峰命名
                                    input_tokens = max(0, usage_data.get("inputTokens", 0))
                                    output_tokens = max(0, usage_data.get("outputTokens", 0))
                                    cache_read_tokens = max(
                                        0, usage_data.get("cacheReadInputTokens", 0)
                                    )
                                    cache_write_tokens = max(
                                        0, usage_data.get("cacheWriteInputTokens", 0)
                                    )

                                    # 计算缓存命中率
                                    total_input = input_tokens + cache_read_tokens
                                    input_cache_hit_rate = (
                                        (cache_read_tokens / total_input * 100)
                                        if total_input > 0
                                        else 0.0
                                    )
                                    # 输出缓存（Bedrock 暂不支持，预留）
                                    cache_read_output = usage_data.get("cacheReadOutputTokens", 0)
                                    total_output = output_tokens + cache_read_output
                                    output_cache_hit_rate = (
                                        (cache_read_output / total_output * 100)
                                        if total_output > 0
                                        else 0.0
                                    )

                                    token_usage.update(
                                        {
                                            "input_tokens": input_tokens,
                                            "output_tokens": output_tokens,
                                            "cache_read_tokens": cache_read_tokens,
                                            "cache_write_tokens": cache_write_tokens,
                                            "input_cache_hit_rate": round(input_cache_hit_rate, 1),
                                            "output_cache_hit_rate": round(
                                                output_cache_hit_rate, 1
                                            ),
                                        }
                                    )

                                    logger.info(
                                        "Token 统计已提取",
                                        extra={
                                            "input": input_tokens,
                                            "output": output_tokens,
                                            "cache_read": cache_read_tokens,
                                            "cache_write": cache_write_tokens,
                                            "input_cache_hit_rate": f"{input_cache_hit_rate:.1f}%",
                                        },
                                    )
                                else:
                                    logger.warning("Result.metrics 没有 accumulated_usage 属性")
                            else:
                                logger.debug("Result 没有 metrics 属性或 metrics 为空")
                        except Exception as e:
                            logger.warning(
                                "Token 统计提取失败",
                                extra={"error": str(e), "error_type": type(e).__name__},
                            )

                    logger.debug(
                        "Yielding Bedrock event (before filter)",
                        extra={"event_keys": list(event.keys())},
                    )
                    filtered_event = filter_event(event)
                    logger.debug(
                        "Yielding filtered event",
                        extra={
                            "filtered_keys": list(filtered_event.keys()),
                            "original_count": len(event.keys()),
                            "filtered_count": len(filtered_event.keys()),
                        },
                    )
                    yield filtered_event
                else:
                    logger.debug(
                        "Skipping non-dict event", extra={"event_type": type(event).__name__}
                    )

            # 流式结束后发送 Token 使用统计
            # ✅ 修复：检查所有 token 类型，避免缓存命中率100%时不发送
            total_tokens = (
                token_usage["input_tokens"]
                + token_usage["output_tokens"]
                + token_usage["cache_read_tokens"]
                + token_usage["cache_write_tokens"]
            )

            if total_tokens > 0:
                yield {
                    "type": "token_usage",
                    "usage": token_usage,
                    "timestamp": time.time(),
                }
                logger.info(
                    "Token 统计事件已发送",
                    extra={
                        "input": token_usage["input_tokens"],
                        "output": token_usage["output_tokens"],
                        "cache_read": token_usage["cache_read_tokens"],
                        "cache_write": token_usage["cache_write_tokens"],
                        "input_cache_hit_rate": f"{token_usage['input_cache_hit_rate']}%",
                        "total_tokens": total_tokens,
                    },
                )

            exec_span.set_status(trace.Status(trace.StatusCode.OK))
            stream_duration = time.time() - stream_start_time
            avg_interval = stream_duration / event_count if event_count > 0 else 0
            logger.info(
                "⏱️ SPAN END: Step 8 - Agent流式执行完成",
                extra={
                    "total_duration_seconds": round(stream_duration, 2),
                    "event_count": event_count,
                    "avg_interval_seconds": round(avg_interval, 3),
                },
            )
        except Exception as e:
            stream_duration = time.time() - stream_start_time
            error_msg = f"Agent execution failed: {str(e)}"
            # 异常降级：清空所有缓存，防止状态污染
            _clear_all_cache(reason=(f"agent_execution_error: " f"{type(e).__name__}: {str(e)}"))
            logger.error(
                "⏱️ SPAN END: Step 8 - Agent流式执行失败",
                extra={
                    "error_msg": error_msg,
                    "error_type": type(e).__name__,
                    "error_details": str(e),
                    "duration_seconds": round(stream_duration, 2),
                    "event_count": event_count,
                },
            )
            import traceback

            logger.error("Agent execution traceback", extra={"traceback": traceback.format_exc()})
            exec_span.set_status(trace.Status(trace.StatusCode.ERROR, error_msg))
            root_span.set_status(trace.Status(trace.StatusCode.ERROR, error_msg))
            yield {"error": str(e), "type": type(e).__name__}
        finally:
            # ✅ GCP 临时凭证文件已废弃（Gateway 模式无需清理）
            if gcp_temp_file:
                logger.warning(
                    "⚠️ 检测到遗留的 GCP 临时凭证文件占位变量", extra={"file": gcp_temp_file}
                )

            if context_token is not None:
                try:
                    context.detach(context_token)
                    logger.debug("OpenTelemetry context detached")
                except Exception as e:
                    logger.error(
                        "Failed to detach OpenTelemetry context",
                        extra={"error": str(e), "error_type": type(e).__name__},
                    )
            # ✅ MCP 连接不再清理（由 microVM 销毁时自动回收）
            invoke_duration = time.time() - invoke_start_time
            breakdown = {}
            if "step7_start_time" in locals():
                step7_dur = locals().get("step7_duration", 0)
                breakdown["agent_creation"] = round(step7_dur, 2)
            if "stream_start_time" in locals():
                stream_dur = time.time() - stream_start_time
                breakdown["streaming"] = round(stream_dur, 2)
            explained_time = sum(breakdown.values())
            unexplained_time = invoke_duration - explained_time
            logger.info(
                "⏱️ INVOKE END - 全局计时总结",
                extra={
                    "total_duration_seconds": round(invoke_duration, 2),
                    "breakdown_seconds": breakdown,
                    "unexplained_seconds": round(unexplained_time, 2),
                    "unexplained_percentage": (
                        round(unexplained_time / invoke_duration * 100, 1)
                        if invoke_duration > 0
                        else 0
                    ),
                },
            )


if __name__ == "__main__":
    '\n    本地测试模式\n\n    运行方式:\n    python agent_runtime.py\n\n    测试命令:\n    curl -X POST http://localhost:8080/invocations       -H "Content-Type: application/json"       -d \'{"prompt": "Hello!"}\'\n'
    import argparse
    import json
    from datetime import datetime

    parser = argparse.ArgumentParser(description="CostQ AgentCore Runtime")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind")
    parser.add_argument("--port", default=8080, type=int, help="Port to bind")
    parser.add_argument("--log-level", default="INFO", help="Log level")
    args = parser.parse_args()
    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[logging.StreamHandler(sys.stderr)],
    )
    startup_log = {
        "timestamp": datetime.now().isoformat(),
        "level": "INFO",
        "message": "🚀 CostQ AgentCore Runtime starting",
        "host": args.host,
        "port": args.port,
        "log_level": args.log_level,
        "model_id": os.getenv("BEDROCK_MODEL_ID"),
        "python_version": sys.version,
        "environment": {
            "AWS_REGION": os.getenv("AWS_REGION"),
            "AWS_DEFAULT_REGION": os.getenv("AWS_DEFAULT_REGION"),
            "BEDROCK_REGION": os.getenv("BEDROCK_REGION"),
            "RDS_SECRET_NAME": os.getenv("RDS_SECRET_NAME"),
            "DOCKER_CONTAINER": os.getenv("DOCKER_CONTAINER"),
            "MEMORY_RESOURCE_ID": os.getenv("MEMORY_RESOURCE_ID"),
            "AGENTCORE_RUNTIME_ARN": os.getenv("AGENTCORE_RUNTIME_ARN"),
            "BEDROCK_MODEL_ID": os.getenv("BEDROCK_MODEL_ID"),
            "AWS_ACCESS_KEY_ID": "***" if os.getenv("AWS_ACCESS_KEY_ID") else None,
            "AWS_SECRET_ACCESS_KEY": "***" if os.getenv("AWS_SECRET_ACCESS_KEY") else None,
            "AWS_SESSION_TOKEN": "***" if os.getenv("AWS_SESSION_TOKEN") else None,
            "ENCRYPTION_KEY": "***" if os.getenv("ENCRYPTION_KEY") else None,
            "BEDROCK_CROSS_ACCOUNT_ROLE_ARN": os.getenv("BEDROCK_CROSS_ACCOUNT_ROLE_ARN"),
        },
    }
    print(json.dumps(startup_log, ensure_ascii=False))
    logger.info("🚀 启动 CostQ AgentCore Runtime")
    logger.info(f"   Host: {args.host}")
    logger.info(f"   Port: {args.port}")
    logger.info(f"   Log Level: {args.log_level}")
    logger.info(f"   Model: {os.getenv('BEDROCK_MODEL_ID')}")
    logger.info("Starting AgentCore Runtime server...")
    print(
        json.dumps(
            {
                "timestamp": datetime.now().isoformat(),
                "level": "INFO",
                "message": "Starting AgentCore Runtime server",
                "host": args.host,
                "port": args.port,
            },
            ensure_ascii=False,
        )
    )
    app.run(host=args.host, port=args.port)
