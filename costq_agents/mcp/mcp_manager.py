"""MCP客户端管理器 - 简化版（无并发限制）

通过STDio方式启动MCP子进程，环境变量传递账号信息，无资源限制和LRU清理。
支持 Gateway MCP 模式（使用 IAM SigV4 认证连接远程 MCP Server）。
"""

import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FutureTimeoutError
from pathlib import Path

import boto3
from botocore.credentials import Credentials
from mcp import StdioServerParameters
from mcp.client.stdio import stdio_client
from strands.tools.mcp import MCPClient

from costq_agents.services.streamable_http_sigv4 import streamablehttp_client_with_sigv4

# 初始化标准 logger
logger = logging.getLogger(__name__)

# 环境判断
IS_PRODUCTION = os.getenv("ENVIRONMENT") == "production"


class MCPManager:
    """MCP客户端管理器（简化版）

    设计理念：
        1. STDio子进程启动（官方标准方式，符合AgentCore Runtime）
        2. 环境变量传递账号信息（TARGET_ACCOUNT_ID/ROLE_NAME）
        3. 串行加载模式（默认）：稳定性优先，适用于资源受限环境
        4. 并行加载模式（可选）：性能优先，需要充足资源
        5. 无LRU清理（Session结束时子进程自动销毁）
        6. 无凭证刷新（MCP Server内部每次调用AssumeRole）

    Loading Strategies:
        - create_all_clients(): 串行加载（50-60秒，稳定，推荐Runtime使用）
        - create_all_clients_parallel(): 并行加载（10-15秒，快速，需充足资源）

    Attributes:
        project_root: 项目根目录路径
        DEFAULT_SERVER_TYPES: 默认MCP服务器类型列表

    Examples:
        >>> manager = MCPManager()
        >>> os.environ["TARGET_ACCOUNT_ID"] = "123456789012"
        >>> os.environ["TARGET_ROLE_NAME"] = "CostQAccessRole"
        >>> # 串行加载（推荐）
        >>> clients = manager.create_all_clients()
        >>> print(len(clients))
        8
    """

    # 默认MCP服务器列表
    DEFAULT_SERVER_TYPES = [
        "common-tools",  # 通用工具集（时间日期等基础功能）
        "alert",  # 平台级告警管理
        "send-email",  # 平台级邮件发送（SES）
    ]

    def __init__(self) -> None:
        """初始化MCP管理器"""
        self.project_root = Path(__file__).parent.parent.parent

        logger.info("MCPManager初始化完成")

    # ==================== Gateway MCP 配置属性 ====================

    @property
    def gateway_url(self) -> str:
        """Gateway MCP URL（从环境变量获取）

        Returns:
            str: Gateway HTTP 端点 URL（如果未配置则返回空字符串）

        Notes:
            - 环境变量名称：COSTQ_AWS_MCP_SERVERS_GATEWAY_URL
            - 示例值：https://xxx.gateway.bedrock-agentcore.ap-northeast-1.amazonaws.com/mcp
        """
        return os.getenv("COSTQ_AWS_MCP_SERVERS_GATEWAY_URL", "")

    @property
    def gateway_service(self) -> str:
        """Gateway MCP 服务名（用于 SigV4 签名）

        Returns:
            str: AWS 服务名称，默认 "bedrock-agentcore"
        """
        return os.getenv("GATEWAY_SERVICE", "bedrock-agentcore")

    @property
    def gateway_region(self) -> str:
        """Gateway MCP 区域（用于 SigV4 签名）

        Returns:
            str: AWS 区域，默认 "ap-northeast-1"
        """
        return os.getenv("AWS_REGION", "ap-northeast-1")

    # ==================== Gateway MCP 方法 ====================

    def _get_aws_credentials(self) -> Credentials:
        """获取 AWS 凭证（自动从 Profile / IAM Role）

        Returns:
            Credentials: botocore Credentials 对象

        Notes:
            - 本地开发：使用 AWS_PROFILE 环境变量指定 Profile
            - 生产环境（EKS/Runtime）：使用 IAM Role（自动）
            - 无需明文配置，安全性高
        """
        session = boto3.Session()
        credentials = session.get_credentials()

        if credentials is None:
            raise ValueError("无法获取 AWS 凭证，请检查 AWS_PROFILE 或 IAM Role 配置")

        # 获取冻结的凭证（防止在使用过程中过期）
        frozen_credentials = credentials.get_frozen_credentials()

        return Credentials(
            access_key=frozen_credentials.access_key,
            secret_key=frozen_credentials.secret_key,
            token=frozen_credentials.token,  # Session Token（如果有）
        )

    def create_gateway_client(
        self,
        gateway_url: str | None = None,
        name: str = "gateway-mcp",
        env_var_name: str = "COSTQ_AWS_MCP_SERVERS_GATEWAY_URL",
    ) -> MCPClient:
        """创建 Gateway MCP 客户端（使用 IAM SigV4 认证）

        使用 streamablehttp_client_with_sigv4 连接远程 Gateway MCP Server。
        凭证自动从 AWS Profile / IAM Role 获取，无需明文传递。

        Args:
            gateway_url: Gateway HTTP 端点 URL
                (默认从环境变量读取)
            name: 客户端名称（用于日志）
            env_var_name: Gateway URL 的环境变量名

        Returns:
            MCPClient: Gateway 客户端（与本地客户端接口一致）

        Raises:
            ValueError: 如果 Gateway URL 未配置或 AWS 凭证获取失败

        Notes:
            - 使用 IAM SigV4 认证（自动获取凭证）
            - 本地开发：使用 AWS_PROFILE 环境变量
            - 生产环境：使用 IAM Role（自动）
        """
        if gateway_url is None:
            gateway_url = os.getenv(env_var_name, "")

        if not gateway_url:
            raise ValueError(
                f"Gateway URL 未配置，请设置环境变量 {env_var_name}\n"
                f"示例: export {env_var_name}=https://xxx.gateway.bedrock-agentcore.ap-northeast-1.amazonaws.com/mcp"
            )

        # 从 boto3 Session 获取凭证（自动使用 Profile 或 IAM Role）
        credentials = self._get_aws_credentials()

        # 获取 SigV4 签名参数
        service = self.gateway_service
        region = self.gateway_region

        # 创建带 SigV4 签名的 transport factory
        def create_transport():
            return streamablehttp_client_with_sigv4(
                url=gateway_url,
                credentials=credentials,
                service=service,
                region=region,
            )

        logger.info(
            "✅ 创建 Gateway MCP 客户端",
            extra={
                "mcp_name": name,
                "gateway_url": gateway_url[:50] + "..." if len(gateway_url) > 50 else gateway_url,
                "service": service,
                "region": region,
                "auth_type": "SigV4"
            }
        )

        return MCPClient(create_transport)

        # 从 boto3 Session 获取凭证（自动使用 Profile 或 IAM Role）
        credentials = self._get_aws_credentials()

        # 获取 SigV4 签名参数
        service = self.gateway_service
        region = self.gateway_region

        # 创建带 SigV4 签名的 transport factory
        def create_transport():
            return streamablehttp_client_with_sigv4(
                url=gateway_url,
                credentials=credentials,
                service=service,
                region=region,
            )

        logger.info(
            "✅ 创建 Gateway MCP 客户端",
            extra={
                "mcp_name": name,
                "gateway_url": gateway_url[:50] + "..." if len(gateway_url) > 50 else gateway_url,
                "service": service,
                "region": region,
                "auth_type": "SigV4"
            }
        )

        return MCPClient(create_transport)

    def get_full_tools_list(self, client: MCPClient) -> list:
        """获取完整工具列表（处理分页）

        Gateway MCP 可能返回大量工具，需要处理分页。
        此方法会自动处理分页，返回完整的工具列表。

        Args:
            client: 已激活的 MCPClient 实例（必须先调用 __enter__）

        Returns:
            list: 完整的工具列表

        Examples:
            >>> manager = MCPManager()
            >>> client = manager.create_gateway_client()
            >>> client.__enter__()  # 激活客户端
            >>> tools = manager.get_full_tools_list(client)  # 获取完整工具列表
            >>> print(f"工具数量: {len(tools)}")
            >>> client.__exit__(None, None, None)

        Notes:
            - 自动处理分页（pagination_token）
            - 适用于 Gateway MCP 和本地 MCP
            - 客户端必须先激活（调用 __enter__）
        """
        tools = []
        pagination_token = None
        truncated_count = 0

        while True:
            # 获取工具列表（带分页）
            result = client.list_tools_sync(pagination_token=pagination_token)

            # ✅ 修复：截断超过64字符的工具名称（Bedrock Converse API 限制）
            for tool in result:
                if len(tool.tool_name) > 64:
                    original_name = tool.tool_name
                    tool.tool_name = tool.tool_name[:64]  # 截断到64字符
                    truncated_count += 1
                    logger.warning(
                        "⚠️  工具名称超过64字符，已截断",
                        extra={
                            "original_name": original_name,
                            "truncated_name": tool.tool_name,
                            "original_length": len(original_name),
                        }
                    )

            tools.extend(result)

            # 检查是否有更多页
            if hasattr(result, "pagination_token") and result.pagination_token:
                pagination_token = result.pagination_token
            else:
                break

        logger.info(
            "✅ 获取完整工具列表",
            extra={
                "tool_count": len(tools),
                "truncated_count": truncated_count,
            }
        )

        return tools

    def _get_env(self, additional_env: dict[str, str] | None = None) -> dict[str, str]:
        """获取MCP子进程的环境变量（支持隔离传递）

        Args:
            additional_env: 额外的环境变量（隔离传递给子进程，不污染主进程）
                           例如: {"AWS_ACCESS_KEY_ID": "...", "AWS_SECRET_ACCESS_KEY": "..."}

        环境变量说明：
            - AWS_ACCESS_KEY_ID: AWS访问密钥（通过additional_env传递，隔离）
            - AWS_SECRET_ACCESS_KEY: AWS密钥（通过additional_env传递，隔离）
            - AWS_SESSION_TOKEN: AWS会话令牌（通过additional_env传递，隔离）
            - AWS_REGION: AWS区域
            - TARGET_ACCOUNT_ID: 目标AWS账号ID（兼容旧方式，MCP自行AssumeRole）
            - TARGET_ROLE_NAME: IAM Role名称（兼容旧方式，MCP自行AssumeRole）

        Returns:
            dict: 环境变量字典

        Notes:
            - ✅ 环境变量隔离：additional_env仅传递给子进程，不修改os.environ
            - ✅ 主进程保持干净：OpenTelemetry等自动使用Runtime IAM Role
            - ✅ 优先使用additional_env传递的凭证（查询账号凭证）
            - ✅ 兼容旧的TARGET_ACCOUNT_ID方式（MCP自行AssumeRole）
            - ✅ Alert和GCP MCP使用平台级凭证
        """
        # ✅ 白名单模式：只传递必要的环境变量（最小权限原则）
        # ⚠️ 不使用 os.environ.copy()，避免泄漏敏感信息（DATABASE_URL, ENCRYPTION_KEY, JWT_SECRET_KEY 等）
        env = {
            "AWS_REGION": os.getenv("AWS_REGION", "us-east-1"),
            "AWS_DEFAULT_REGION": os.getenv("AWS_DEFAULT_REGION", "us-east-1"),
            "FASTMCP_LOG_LEVEL": "ERROR",  # 减少 MCP 日志噪音
        }

        # 传递必要的系统环境变量（MCP 子进程运行所需）
        for key in ["PATH", "HOME", "USER", "LANG", "LC_ALL", "PYTHONPATH"]:
            value = os.getenv(key)
            if value:
                env[key] = value

        # ========== 传递 uvx 缓存环境变量（关键优化）==========
        # uvx 需要这些变量才能找到并使用预装的 MCP 工具
        # 不传递会导致 uvx 重新下载 botocore 等依赖（13.9MB）
        uv_tool_dir = os.getenv("UV_TOOL_DIR")
        uv_tool_bin_dir = os.getenv("UV_TOOL_BIN_DIR")

        if uv_tool_dir:
            env["UV_TOOL_DIR"] = uv_tool_dir
            logger.debug(f"✅ 传递 UV_TOOL_DIR: {uv_tool_dir}")
        else:
            logger.warning("⚠️ UV_TOOL_DIR 未设置，uvx 将重新下载依赖")

        if uv_tool_bin_dir:
            env["UV_TOOL_BIN_DIR"] = uv_tool_bin_dir
            logger.debug(f"✅ 传递 UV_TOOL_BIN_DIR: {uv_tool_bin_dir}")
        else:
            logger.warning("⚠️ UV_TOOL_BIN_DIR 未设置")

        # 传递 DOCKER_CONTAINER 标志（用于 Send Email 等平台级 MCP 判断环境）
        if os.getenv("DOCKER_CONTAINER"):
            env["DOCKER_CONTAINER"] = os.getenv("DOCKER_CONTAINER")

        # 传递 PLATFORM_AWS_PROFILE（本地开发环境使用）
        if os.getenv("PLATFORM_AWS_PROFILE"):
            env["PLATFORM_AWS_PROFILE"] = os.getenv("PLATFORM_AWS_PROFILE")

        # ========== ✅ 【关键修改】环境变量隔离传递 ==========
        # 优先使用 additional_env 传递的查询账号凭证（隔离传递，不污染主进程）
        if additional_env:
            env.update(additional_env)
            logger.debug(
                "✅ 隔离传递额外环境变量",
                extra={"additional_env_count": len(additional_env)}
            )

        # 兼容旧方式：如果 additional_env 未提供，则从 os.environ 读取
        # 注意：这是为了向后兼容，新代码应该使用 additional_env 参数
        else:
            # 优先传递 Runtime 已经获取的临时凭证
            # 注意：只有当值不为 None 时才添加，因为 StdioServerParameters.env 要求 dict[str, str]
            if os.getenv("AWS_ACCESS_KEY_ID"):
                env["AWS_ACCESS_KEY_ID"] = os.getenv("AWS_ACCESS_KEY_ID")
                if os.getenv("AWS_SECRET_ACCESS_KEY"):
                    env["AWS_SECRET_ACCESS_KEY"] = os.getenv("AWS_SECRET_ACCESS_KEY")
                if os.getenv("AWS_SESSION_TOKEN"):
                    env["AWS_SESSION_TOKEN"] = os.getenv("AWS_SESSION_TOKEN")
                # 同时设置 AWS_DEFAULT_REGION 确保兼容性
                env["AWS_DEFAULT_REGION"] = os.getenv("AWS_DEFAULT_REGION") or env["AWS_REGION"]

            # 兼容旧的 TARGET_ACCOUNT_ID 方式（MCP 自行 AssumeRole）
            if os.getenv("TARGET_ACCOUNT_ID"):
                env["TARGET_ACCOUNT_ID"] = os.getenv("TARGET_ACCOUNT_ID")
            if os.getenv("TARGET_ROLE_NAME"):
                env["TARGET_ROLE_NAME"] = os.getenv("TARGET_ROLE_NAME")

        return env


    def create_common_tools_client(self, additional_env: dict[str, str] | None = None) -> MCPClient:
        """创建Common Tools MCP客户端（通用工具集）

        提供跨平台的通用工具，包括：
        - get_today_date: 获取当前日期信息（多种格式）
        - 替代 AWS Cost Explorer 的 get_today_date 工具

        Args:
            additional_env: 额外的环境变量（隔离传递给子进程）

        Returns:
            MCPClient: Common Tools 客户端

        Notes:
            - 使用本地 Python 实现（无外部依赖）
            - 不需要 AWS/GCP 凭证（纯工具函数）
            - 适用于所有平台（AWS、GCP 通用）
        """
        server_params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "costq_agents.mcp.common_tools_mcp_server.server"],
            cwd=str(self.project_root),
            env={
                **self._get_env(additional_env),
                "PYTHONPATH": str(self.project_root),
            },
        )
        return MCPClient(lambda: stdio_client(server_params))

    def create_alert_client(self, additional_env: dict[str, str] | None = None) -> MCPClient:
        """创建Alert MCP客户端（使用平台级凭证）

        Args:
            additional_env: 额老的环境变量（隔离传递给子进程）
        """
        server_params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "costq_agents.mcp.alert_mcp_server.server"],
            cwd=str(self.project_root),
            env={
                **self._get_env(additional_env),
                "PYTHONPATH": str(self.project_root),
            },
        )
        return MCPClient(lambda: stdio_client(server_params))

    def create_send_email_client(self, additional_env: dict[str, str] | None = None) -> MCPClient:
        """创建Send Email MCP客户端（邮件发送服务）

        使用平台级凭证，专注于邮件发送功能。

        Args:
            additional_env: 额外的环境变量（隔离传递给子进程）

        Returns:
            MCPClient: Send Email客户端

        Notes:
            - 使用平台级AWS凭证（AWS_PROFILE或IAM Role）
            - 不需要TARGET_ACCOUNT_ID（邮件发送是平台级功能）
            - 使用AWS SES发送邮件
        """
        server_params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "costq_agents.mcp.send_email_mcp_server.server"],
            cwd=str(self.project_root),
            env={
                **self._get_env(additional_env),
                "PYTHONPATH": str(self.project_root),
            },
        )
        return MCPClient(lambda: stdio_client(server_params))

    def create_gcp_gateway_client(
        self,
        gateway_url: str | None = None,
        name: str = "gcp-gateway-mcp",
    ) -> MCPClient:
        """创建 GCP Gateway MCP 客户端（使用 IAM SigV4 认证）"""
        return self.create_gateway_client(
            gateway_url=gateway_url,
            name=name,
            env_var_name="COSTQ_GCP_MCP_SERVERS_GATEWAY_URL",
        )

    def _get_client_factory(self, server_type: str):
        """获取MCP客户端工厂方法（消除代码重复）

        Args:
            server_type: MCP服务器类型

        Returns:
            Callable | None: 工厂函数，如果类型未知返回None

        Examples:
            >>> manager = MCPManager()
            >>> factory = manager._get_client_factory("common-tools")
            >>> client = factory()

        Notes:
            - 使用字典映射替代长串if-elif
            - 新增MCP类型只需在factory_map添加一行
            - 串行和并行模式共享此工厂方法
        """
        factory_map = {
            "common-tools": self.create_common_tools_client,
            "alert": self.create_alert_client,
            "send-email": self.create_send_email_client,
            "gcp-gateway": self.create_gcp_gateway_client,
        }
        return factory_map.get(server_type)

    def _create_and_activate_client(
        self,
        server_type: str,
        additional_env: dict[str, str] | None = None  # ✅ 添加环境变量隔离支持
    ) -> tuple[str, MCPClient | None, str | None, float]:
        """创建并激活单个MCP客户端（线程池执行，带计时，支持环境变量隔离）

        Args:
            server_type: MCP服务器类型
            additional_env: 额外的环境变量（隔离传递给MCP子进程）

        Returns:
            Tuple[str, MCPClient | None, str | None, float]:
                (server_type, client, error_message, duration_seconds)

        Notes:
            - ✅ 环境变量隔离：additional_env仅传递给子进程，不修改os.environ
            - 在ThreadPoolExecutor的工作线程中执行
            - 异常会被捕获并返回错误信息
            - ✅ 新增：返回单个MCP初始化耗时
        """
        # ⏱️ 单个MCP初始化计时
        mcp_start = time.time()

        try:
            # 使用工厂方法获取创建函数
            factory = self._get_client_factory(server_type)
            if factory is None:
                duration = time.time() - mcp_start
                return (server_type, None, f"Unknown server type: {server_type}", duration)

            # 创建客户端（传递环境变量隔离参数）
            client = factory(additional_env)
            if client is None:
                duration = time.time() - mcp_start
                return (server_type, None, "Client creation returned None", duration)

            # 激活客户端（启动子进程）
            client.__enter__()

            duration = time.time() - mcp_start

            # ✅ 记录单个MCP初始化成功
            logger.info(
                f"⏱️ MCP初始化成功: {server_type}",
                extra={
                    "server_type": server_type,
                    "duration_seconds": round(duration, 3),
                    "status": "success"
                }
            )

            return (server_type, client, None, duration)

        except Exception as e:
            duration = time.time() - mcp_start
            import traceback
            error_msg = f"{str(e)}\n{traceback.format_exc()}"

            # ✅ 记录单个MCP初始化失败
            logger.error(
                f"⏱️ MCP初始化失败: {server_type}",
                extra={
                    "server_type": server_type,
                    "duration_seconds": round(duration, 3),
                    "status": "failed",
                    "error": str(e)
                }
            )

            return (server_type, None, error_msg, duration)

    def create_all_clients_parallel(
        self,
        server_types: list[str] | None = None,
        max_workers: int = 10,
        per_client_timeout: float = 20.0,
        total_timeout: float = 60.0,
        additional_env: dict[str, str] | None = None,  # ✅ 添加环境变量隔离支持
    ) -> dict[str, MCPClient]:
        """并行创建所有MCP客户端（性能优化版本，支持环境变量隔离）

        使用ThreadPoolExecutor并行启动所有MCP子进程，显著减少初始化时间。

        Args:
            server_types: MCP服务器类型列表（None=使用默认列表）
            max_workers: 最大并行数（默认10，支持9个AWS MCP同时启动）
            per_client_timeout: 单个客户端超时时间（秒）
                - 默认20秒：考虑uvx首次下载Python包和远程连接延迟
                - 后续启动通常只需1-3秒（使用缓存）
            total_timeout: 总超时时间（秒）
                - 默认60秒：允许所有MCP都有足够时间完成
            additional_env: 额外的环境变量（隔离传递给所有MCP子进程，不污染主进程）
                           例如: {"AWS_ACCESS_KEY_ID": "...", "AWS_SECRET_ACCESS_KEY": "..."}

        Returns:
            Dict[str, MCPClient]: 成功创建的客户端字典

        Raises:
            不抛出异常，失败的MCP会记录错误日志并跳过

        Examples:
            >>> # AWS场景：并行创建9个MCP
            >>> manager = MCPManager()
            >>> clients = manager.create_all_clients_parallel()
            >>> print(f"成功创建 {len(clients)} 个MCP")

            >>> # GCP场景：使用 Gateway MCP
            >>> clients = manager.create_all_clients_parallel(
            ...     server_types=["gcp-gateway"]
            ... )

        Notes:
            - AWS场景：3个MCP并行启动（common-tools/alert/send-email）
            - GCP场景：使用 Gateway MCP
            - 失败的MCP会记录错误日志但不中断流程
            - 所有客户端都会自动激活（调用__enter__）
            - 使用环境变量传递凭证，确保在调用前已设置
        """
        if server_types is None:
            server_types = self.DEFAULT_SERVER_TYPES

        start_time = time.time()
        clients: dict[str, MCPClient] = {}
        errors: dict[str, str] = {}
        mcp_timings: dict[str, float] = {}  # ✅ 新增：记录每个MCP的耗时

        logger.info(f"🚀 并行初始化 {len(server_types)} 个 MCP 客户端...")

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            # 提交所有任务（传递环境变量隔离参数）
            future_to_type = {
                executor.submit(self._create_and_activate_client, st, additional_env): st
                for st in server_types
            }

            # 收集结果（带总超时）
            try:
                for future in as_completed(future_to_type, timeout=total_timeout):
                    server_type = future_to_type[future]  # 先获取server_type

                    try:
                        _, client, error, duration = future.result(timeout=per_client_timeout)  # ✅ 接收duration

                        # ✅ 记录单个MCP耗时
                        mcp_timings[server_type] = duration

                        if client is not None:
                            clients[server_type] = client
                            elapsed = time.time() - start_time
                            logger.info(
                                "MCP客户端连接成功",
                                extra={
                                    "server_type": server_type,
                                    "elapsed_seconds": round(elapsed, 2),
                                },
                            )
                        else:
                            errors[server_type] = error or "Unknown error"
                            logger.error(f"  ❌ {server_type}: {error}")

                    except FutureTimeoutError:
                        errors[server_type] = f"Timeout after {per_client_timeout}s"
                        logger.error(f"  ⏱️  {server_type} 超时 ({per_client_timeout}s)")

                    except Exception as e:
                        errors[server_type] = str(e)
                        logger.error(f"  ❌ {server_type} 异常: {e}", exc_info=True)

            except FutureTimeoutError:
                logger.error(f"⏱️  总超时 ({total_timeout}s)", exc_info=True)
                for future, server_type in future_to_type.items():
                    if not future.done():
                        errors[server_type] = "Total timeout"
                        future.cancel()
                        logger.warning(f"  ⚠️  {server_type} 被取消")

            except Exception as e:
                logger.error(f"❌ MCP并行创建过程中出现严重异常: {e}", exc_info=True)
                # 标记所有未完成的任务为失败
                for future, server_type in future_to_type.items():
                    if server_type not in clients and server_type not in errors:
                        errors[server_type] = f"Interrupted by exception: {e}"
                        logger.error(f"  ❌ {server_type} 未完成（被中断）")

        elapsed = time.time() - start_time

        # ✅ 计算MCP初始化统计
        if mcp_timings:
            slowest_mcp = max(mcp_timings.items(), key=lambda x: x[1])
            avg_duration = sum(mcp_timings.values()) / len(mcp_timings)
        else:
            slowest_mcp = ("N/A", 0.0)
            avg_duration = 0.0

        # 详细的完成日志
        success_types = list(clients.keys())
        failed_types = list(errors.keys())
        logger.info(
            "⏱️ MCP并行创建完成",
            extra={
                "success": len(clients),
                "failed": len(errors),
                "total_elapsed_seconds": round(elapsed, 2),  # ✅ 改名：总耗时
                "avg_per_mcp_seconds": round(avg_duration, 3),  # ✅ 新增：平均耗时
                "slowest_mcp": slowest_mcp[0],  # ✅ 新增：最慢MCP
                "slowest_mcp_duration": round(slowest_mcp[1], 3),  # ✅ 新增：最慢MCP耗时
                "success_types": success_types,
                "failed_types": failed_types,
                "individual_timings": {k: round(v, 3) for k, v in mcp_timings.items()},  # ✅ 新增：单个耗时
            },
        )

        # 如果有失败的MCP，记录详细错误
        if errors:
            for server_type, error_msg in errors.items():
                logger.error(f"MCP创建失败详情 - {server_type}: {error_msg}")

        return clients

    def create_all_clients(
        self,
        server_types: list[str] | None = None,
        additional_env: dict[str, str] | None = None
    ) -> dict[str, MCPClient]:
        """创建所有MCP客户端（串行，稳定性优先，支持环境变量隔离）

        Args:
            server_types: MCP服务器类型列表（None=使用默认列表）
            additional_env: 额外的环境变量（隔离传递给所有MCP子进程，不污染主进程）
                           例如: {"AWS_ACCESS_KEY_ID": "...", "AWS_SECRET_ACCESS_KEY": "..."}

        Returns:
            Dict[str, MCPClient]: 客户端字典 {server_type: client}

        Notes:
            - ✅ 串行加载：一个接一个，避免资源竞争
            - ✅ 单个失败不影响其他：错误隔离
            - ✅ 详细日志：记录每个MCP的启动时间
            - ✅ 环境变量隔离：additional_env仅传递给子进程，主进程保持干净
            - ⚠️ 速度较慢：预计50-60秒（vs 并行10-15秒）
            - 适用场景：AgentCore Runtime等资源受限环境
        """
        if server_types is None:
            server_types = self.DEFAULT_SERVER_TYPES

        start_time = time.time()
        clients: dict[str, MCPClient] = {}
        errors: dict[str, str] = {}
        mcp_timings: dict[str, float] = {}  # ✅ 新增：记录每个MCP的耗时

        logger.info(
            f"🔄 串行初始化 {len(server_types)} 个 MCP 客户端（环境变量隔离传递）",
            extra={
                "env_isolation_enabled": additional_env is not None,
                "additional_env_count": len(additional_env) if additional_env else 0
            }
        )

        for idx, server_type in enumerate(server_types, 1):
            mcp_start = time.time()  # ✅ 记录单个MCP启动时间

            try:
                # 根据类型创建客户端（传递隔离的环境变量）
                client: MCPClient | None = None
                if server_type == "common-tools":
                    client = self.create_common_tools_client(additional_env)
                elif server_type == "alert":
                    client = self.create_alert_client(additional_env)
                elif server_type == "send-email":
                    client = self.create_send_email_client(additional_env)
                elif server_type == "gcp-gateway":
                    client = self.create_gcp_gateway_client()
                else:
                    logger.warning("未知MCP类型，跳过", extra={"server_type": server_type})
                    continue

                if client is None:
                    errors[server_type] = "Client creation returned None"
                    logger.warning(f"  ⚠️  [{idx}/{len(server_types)}] {server_type} 创建失败（返回None）")
                    continue

                # 激活客户端（启动子进程）
                client.__enter__()
                clients[server_type] = client

                # ✅ 记录单个MCP耗时
                mcp_duration = time.time() - mcp_start
                mcp_timings[server_type] = mcp_duration

                # ✅ 详细进度日志（生产环境也显示）
                elapsed_total = time.time() - start_time
                logger.info(
                    f"  ✅ [{idx}/{len(server_types)}] {server_type}",
                    extra={
                        "mcp_duration_seconds": round(mcp_duration, 2),
                        "total_elapsed_seconds": round(elapsed_total, 2),
                        "progress_percent": round(idx / len(server_types) * 100, 1)
                    }
                )

            except ValueError as e:
                errors[server_type] = f"ValueError: {str(e)}"
                logger.error(
                    f"  ❌ [{idx}/{len(server_types)}] {server_type} 参数验证失败",
                    extra={"error": str(e)}
                )
            except Exception as e:
                import traceback
                errors[server_type] = f"Exception: {str(e)}"
                logger.error(
                    f"  ❌ [{idx}/{len(server_types)}] {server_type} 启动异常",
                    exc_info=True,
                    extra={
                        "error": str(e),
                        "traceback": traceback.format_exc()
                    }
                )

        # ✅ 详细的完成日志
        elapsed = time.time() - start_time
        success_types = list(clients.keys())
        failed_types = list(errors.keys())

        # 计算统计信息
        if mcp_timings:
            slowest_mcp = max(mcp_timings.items(), key=lambda x: x[1])
            fastest_mcp = min(mcp_timings.items(), key=lambda x: x[1])
            avg_duration = sum(mcp_timings.values()) / len(mcp_timings)
        else:
            slowest_mcp = ("N/A", 0.0)
            fastest_mcp = ("N/A", 0.0)
            avg_duration = 0.0

        logger.info(
            "⏱️  串行MCP创建完成",
            extra={
                "success": len(clients),
                "failed": len(errors),
                "total_elapsed_seconds": round(elapsed, 2),
                "avg_per_mcp_seconds": round(avg_duration, 2),
                "slowest_mcp": slowest_mcp[0],
                "slowest_mcp_duration": round(slowest_mcp[1], 2),
                "fastest_mcp": fastest_mcp[0],
                "fastest_mcp_duration": round(fastest_mcp[1], 2),
                "success_types": success_types,
                "failed_types": failed_types,
                "individual_timings": {k: round(v, 2) for k, v in mcp_timings.items()},
            },
        )

        # 如果有失败的MCP，记录详细错误
        if errors:
            for server_type, error_msg in errors.items():
                logger.error(f"MCP创建失败详情 - {server_type}: {error_msg}")

        return clients

    def close_all_clients(self, clients: dict[str, MCPClient]) -> None:
        """关闭所有MCP客户端

        Args:
            clients: 客户端字典

        Examples:
            >>> clients = manager.create_all_clients()
            >>> # ... 使用clients ...
            >>> manager.close_all_clients(clients)

        Notes:
            - 调用每个客户端的__exit__方法
            - 失败时记录错误但不中断流程
        """
        for server_type, client in clients.items():
            try:
                client.__exit__(None, None, None)  # type: ignore[arg-type]
                logger.info(
                    "MCP客户端已关闭",
                    extra={"server_type": server_type},
                )
            except Exception:
                logger.error(
                    "MCP客户端关闭失败",
                    extra={"server_type": server_type},
                    exc_info=True
                )
