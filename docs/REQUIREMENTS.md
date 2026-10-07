# Homework 1 要求对应

需求来源：用户提供的 `Homework_1.pptx`，共 4 页。仅将作业内容用于确定实现和交付范围。作者：2412190104 唐佳杰。

| PPT 要求 / 评分项 | 对应实现与证据 |
| --- | --- |
| 简单代码助手 Agent | 选择“代码审查 Agent”；真实模型 + 文件/AST/测试工具 |
| LLM 调用、Prompt 设计、工具集成 | `providers.py`、`prompts.py`、`tools.py` |
| 输入 → 推理 → 工具调用 → 输出 | `agent.py` 的共享循环，JSON 事件与 Web 行动轨迹 |
| 至少一种工具 | 四种工具：列表、读取、静态分析、可选测试执行 |
| CLI 或简单 Web | 两种均实现；网页启动配置模型、选择本地目录/文件；另有多轮 `chat` |
| 主流框架或原生 API | 原生 Responses 与 Chat Completions HTTP API |
| 支持上下文记忆 | 完整轮次内存、CLI 会话持久化、Web 会话 |
| 错误处理与重试 | 工具失败观察、API 有限重试、预算停止、失败回滚 |
| 功能完整性（40%） | 可运行示例、同一套回归测试、路径/大小/超时等边界处理 |
| Agent 架构（30%） | Provider / Loop / Tools / Memory 分层，共用 CLI/Web 装配 |
| 代码质量（20%） | 类型标注、独立模块、自动测试、跨平台 CI |
| 文档（10%） | README、DESIGN、Desgin 入口、演示与验证记录 |
| 源码仓库 | [dankingzzzing/Code-Agent](https://github.com/dankingzzzing/Code-Agent) |
| README / 设计文档 | 根目录中提供并随压缩包打包 |
| 可选 1 分钟演示视频 | `docs/assets/demo.mp4` 实际操作录像，另附 `docs/DEMO.md` 讲解步骤 |
| 学号姓名命名，压缩包 < 200M | `scripts/package_submission.py` 自动生成 `2412190104-唐佳杰.zip` 并检查大小 |
| 提交到 `001Homework1` | 生成可提交压缩包；课程平台提交需要在该平台完成 |
| 截止时间 | PPT 标注 2026-10-07 24:00（Asia/Shanghai），即 2026-10-08 00:00 |

演示模式明确标识“非 LLM”。网页会实际验证模型工具调用；本次更新也使用本机用户已配置的服务验证了真实云端审查。个人密钥不进入交付内容。
