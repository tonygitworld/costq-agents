# 执行规范

- 当我说“提交到git”、“推送到git”、“提交并推送到git”、“提交”等指令时，提交并推送本地代码到git仓库
- 严禁在没有得到我的明确指示的情况下，修改任何代码
- 严谨的对待工作，不能靠推测，分析问题要有理有据，实事求事，不确定的问题联网搜索解决方案或者和我确认
- 在任何地方的命名使用costq，而不是strands-agent-demo相关的文字
- 按照程序编码最佳实践修改代码，确保没有冗余逻辑，简单问题不复杂化，不侵入任何现有逻辑，不影响现有代码功能，避免过度设计。
- 如果必须要修改现有的逻辑要和我说明潜在影响并和我沟通
- 每次修改后必须按照编程最佳实践立即语法、缩进、代码逻辑等检查
- Always use Context7 MCP when I need library/API documentation, code generation, setup or configuration steps without me having to explicitly ask.
- Always use semgrep MCP to scan code for security vulnerabilities.


# 文档规范

- 将需求文档、任务文档、临时文档、测试脚本等临时文件创建到 costq/docs/ 目录下，需要在 costq/docs/ 下创建和主题相应的以日期戳开头的子目录如：“20261121_流式输出功能”
- 如果在测试过程中生成了临时测试脚本，**必须在测试之后删除**


# 编码规范

- 编程规范参考 CODING_STANDARDS.md
- 零侵入性原则：仅修改目标代码，不改变业务逻辑和函数签名，完美隔离不影响现有功能
- 分步执行验证：将大型重构拆分为独立阶段（Phase 1/2/3），每个阶段都可以独立验证和回滚
- 避免过度设计："少即是多"，使用最简单的方案，避免双重系统和冗余代码
- 基于实际代码而非假设：必须实际验证代码，不要基于猜测分析（如MCP预热机制分析教训）
- 保持谦虚验证假设：要联网搜索验证假设，采纳最佳方案
- 完整验证流程：每步都要验证，代码级验证+部署后验证+功能验证+安全验证
- 详细的执行计划和Checklist：清晰的步骤、预期输出、验证点，失败可快速定位
- 代码批量修改的教训：不要使用sed直接修改文件，因为sed无法精确控制插入位置且容易出错。更好的方案是使用Python脚本先生成diff预览，手动审查确认后再应用修改。loguru迁移到logging时必须检查的特有语法包括：logger.opt(exception=True)要改为exc_info=True、logger.bind()要改为extra={}参数、logger.catch()装饰器要改为标准异常处理。
- 凡是在方案、编码过程遇到任何争议或不确定，必须在第一时间主动告知我由我做决策。
- 对于需要补充的信息，即使向我询问，而不是直接应用修改。
- 每次改动基于最小范围修改原则。


# AWS 命令行调试程序使用的参数

## 通用参数

- ** Profile: 3532 **
- ** Region: ap-northeast-1 **

## agentcore cli 命令：aws bedrock-agentcore-control


## agentcore runtime ID：

- 生产环境：cosq_agents_production-JY4CiUDPvV

## agentcore memory ID：

- 生产环境：CostQ_Pro-77Jh0OAr3A
- 开发环境：CostQ_Dev-Su0pSXBOca

## agentcore 的 cloudwatch 日志组：

1. 生产环境：

1.1. runtime：
- /aws/bedrock-agentcore/runtimes/cosq_agents_production-JY4CiUDPvV-DEFAULT
- /aws/vendedlogs/bedrock-agentcore/runtime/USAGE_LOGS/cosq_agents_production-JY4CiUDPvV 
- /aws/vendedlogs/bedrock-agentcore/runtime/APPLICATION_LOGS/cosq_agents_production-JY4CiUDPvV
- /aws/vendedlogs/bedrock-agentcore/workload-identity-directory/APPLICATION_LOGS/default
1.2 memory：
- /aws/vendedlogs/bedrock-agentcore/memory/APPLICATION_LOGS/CostQ_Pro-77Jh0OAr3A

2. 开发环境

1.1 runtime：
/aws/bedrock-agentcore/runtimes/cosq_agents_development_lyg-dpj4zV9FKE-DEFAULT

3. trace 日志组：
- aws/spans