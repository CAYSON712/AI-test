---
name: test-runner
description: AI 测试「执行测试」专用入口。当用户需要对已生成的数据集（YAML）执行测试、打分并追踪 trace 时使用。依赖 trace 平台运行中，调用执行器（Mock / 真实 MCP）把用例翻译成对被测系统的调用。适用于 AI POS、客流统计、AI 摄像头等系统的能力测试执行。
---

# 执行测试

## Overview

本 Skill 读取已生成的数据集 YAML，调用执行器执行用例、打分、上报 trace 平台并查看结果。工作目录为 `test-runner/`。

- **脚本位置**：`test-runner/scripts/run_dataset.py`（执行入口）
- **执行器**：`test-runner/executors/`（Mock / 真实 POS MCP）
- **trace 平台**：`trace_platform/`（存储 + 展示）
- **依赖**：trace 平台运行中；数据集来自 `dataset-generator` skill 的产出

## 流程

| 步骤 | 做什么 | 脚本/位置 | 依赖 |
|---|---|---|---|
| ① 启动平台 | 启动 trace 平台 | `trace_platform/`（uvicorn） | — |
| ② 执行测试 | 读数据集 → 执行器 → 打分 → 上报 | `test-runner/scripts/run_dataset.py` | trace 平台运行中 |
| ③ 看 trace | 查看 trace 树、打分、定位问题 | 浏览器 `http://127.0.0.1:8000` | trace 平台运行中 |

## 执行步骤

### Step 1: 启动 trace 平台（执行测试前必做）
```powershell
cd 20260805013102/trace_platform
python -m uvicorn app:app --port 8000
```

### Step 2: 执行测试（读生成端产出的数据集）
```powershell
cd 20260805013102/test-runner/scripts
python run_dataset.py --yaml ../../dataset-generator/datasets/<数据集名>.yaml
```

### Step 3: 看结果
浏览器打开 `http://127.0.0.1:8000`。

## 执行器说明（`test-runner/executors/`）

| 文件 | 说明 |
|---|---|
| `base.py` | 执行器基类 + ExecResult |
| `mock_executor.py` | Mock 执行器（默认，读生成端能力目录） |
| `pos_mcp_executor.py` | 真实 POS MCP 执行器（含鉴权） |
| `registry.py` | 执行器注册表（按能力路由） |

- 默认只启用 Mock 执行器。
- 接真实 POS MCP：先设置环境变量 `POS_MCP_URL / POS_MCP_TOKEN / POS_MCP_COMPANY_ID`，再运行 `python test-runner/executors/pos_mcp_executor.py` 验证连接；配置了 token 后 `run_dataset.py` 才会路由到真实执行器。

## 相关文件

- **数据集**：`dataset-generator/datasets/*.yaml`（由 `dataset-generator` skill 产出）
- **能力目录**：`dataset-generator/ability/*.yaml`（两端共享的单一数据源，执行器引用它）
- **trace 平台**：`trace_platform/app.py`（FastAPI 存储+展示）
- **打分**：`test-runner/scorer.py`（0.0~1.0 连续分），被 `run_dataset.py` 依赖

## 注意

- token 属敏感信息，建议用环境变量 `POS_MCP_TOKEN`，勿硬编码提交 git
- 本机有代理劫持，LLM/网络调用已内部处理
- 打分当前用规则，接真实 MCP 后可细化
