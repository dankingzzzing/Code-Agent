# 一分钟演示步骤

启动：`python -m code_agent serve --demo --allow-exec`，打开 `http://127.0.0.1:8765`。

已提供真实浏览器操作录像（无配音，41.92 秒）：[demo.mp4](assets/demo.mp4)。录像依次展示含缺陷示例、带行号的建议、失败测试，以及修复示例的六个测试通过。也可按下面的步骤现场讲解。

| 时间 | 操作 / 讲解 |
| --- | --- |
| 0–8 秒 | 展示名称、作者和模式。说明离线演示使用规则规划器，真实模式可接原生 LLM API。 |
| 8–20 秒 | 选择 `examples/buggy`，点击“开始审查”，展示文件读取、AST 与测试调用。 |
| 20–35 秒 | 展示可变默认参数、动态执行、异常吞掉和真实测试失败；每个静态发现有文件与行号。 |
| 35–48 秒 | 切换到 `examples/fixed` 并审查，展示同一组六个回归测试全部通过。 |
| 48–58 秒 | 展示行动轨迹、报告下载和 README；指出 CLI 支持会话记忆、真实模式有错误重试。 |

命令行备选演示：

```powershell
python -m code_agent review examples/buggy --demo --allow-exec --trace
python -m code_agent review examples/fixed --demo --allow-exec --trace
```

含缺陷示例的测试失败是预期演示结果，项目自身测试与修复示例应通过。不要把离线演示描述为已经调用真实云端模型。
