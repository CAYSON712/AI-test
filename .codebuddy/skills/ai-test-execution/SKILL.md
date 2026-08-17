---
name: ai-test-execution
description: AI 测试「测试执行」专用入口。当用户需要对已生成的数据集执行测试时，调用执行器（Mock/真实 MCP）跑用例，基于 Rubric 5 分制评分，统计通过率 + 置信区间。适用于任何可被测的 AI 系统。
---

# AI 测试执行

## Overview

本 Skill 读取数据集，用执行器（Mock / 真实 MCP）跑用例，Rubric 评分 + 统计判定。**不绑定任何具体系统**，通过"系统配置"适配任意 AI 系统。

- **框架**：`ai-test-framework/`
- **执行器**：`ai-test-framework/executors/`（Mock / 配置驱动的通用真实执行器）
- **评分**：`ai-test-framework/rubric/`（Rubric 5 分制 + LLM-as-Judge）
- **执行入口**：`ai-test-framework/scripts/run_test.py`

## 前置条件（执行前必读）

**接数据集后，先确认以下条件再跑，否则结果无意义**：

| 前置条件 | 是否必须 | 说明 |
|---|---|---|
| **数据集** | ✅ 必须 | 先由「需求分析」skill 生成，否则无用例可跑 |
| **系统配置** | ⚠️ 跑 real 时 | 需 `configs/<系统>.yaml` + `ability/能力目录_<系统>.yaml`；缺失时优雅降级到 Mock |
| **真实系统可用** | ⚠️ 跑 real 时 | 真实执行器需被测系统在线、`.env` 的 token 有效 |
| **Mock 零配置** | — | Mock 执行器隔离副作用，无需任何配置 |

**操作指引**：
- 只测 Agent/Skill 决策 → 用 `mock`，零配置直接跑
- 测真实 E2E（Agent+真实 MCP）→ 用 `real`，**先确认 `.env` 已配 token、系统配置齐全**，且被测系统在线

## 五类需求类型的测试方式（A-E）

| 类型 | 测什么 | 接入方式 | 当前支持 |
|---|---|---|---|
| **A** MCP 工具 | 纯工具本身的参数/返回/边界/异常 | 直连 MCP 工具 | ✅（generic 直连） |
| **B** Agent 系统 | Agent 的意图识别/上下文/鲁棒性 | 对话式（mock 或真实对话） | ✅ |
| **C** Agent+MCP 集成 | 意图→选工具→参数→执行的完整链路 | 对话式 + 真实 MCP ★ | ✅（配置驱动） |
| **D** Skill 原子能力 | 单原子子能力的行为边界 | 直连能力 | ✅ |
| **E** RAG/知识库 | 检索相关性 / 答案忠实度 | RAG 检索+生成 | ✅（配置驱动） |

> 说明：
> - 通用真实执行器 `generic_mcp_executor` 覆盖 A/C/D 的直连与对话接入（按需求类型 + 系统配置驱动）
> - RAG 执行器 `generic_rag_executor` 覆盖 E（检索+生成），按 E 类维度表（召回率/精准率/幻觉率/时效性/Groundedness）评分
> - A-E 五类已全部支持
>
> ⚠️ **注意**：`configs/客服知识库.yaml`、`ability/能力目录_客服知识库.yaml`、`datasets/E_客服知识库.yaml` 均为**占位样例**，用于验证 E 类链路，**非真实系统**。真实接入请按被测知识库改写。

## 通用化执行器（配置驱动）

**核心**：真实执行器不写死任何系统，靠两份配置驱动：

```
ai-test-framework/
├── configs/<系统>.yaml        # 连接 + 工具 schema + verify 配置
└── ability/能力目录_<系统>.yaml # 能力→工具映射 + verify 规则
```

**新增一个系统（如客流统计）**：
1. 复制 `configs/POS_商品管理.yaml` → `configs/<新系统>.yaml`，填连接 + mcp_tools
2. 准备 `ability/能力目录_<新系统>.yaml`（Task 结构：能力/操作类型/成功标准(db|语义|拒绝)/负向场景）
3. 无需写任何 Python 逻辑，执行器自动加载

> **能力目录新旧兼容**：新版用「成功标准」声明校验（db 查字段 / 语义看输出 / 拒绝应拦截）；
> 旧版 `verify_tool/verify_field/verify_expect` 字段仍被兼容读取。执行器会从「成功标准」自动提取 db 校验配置。

系统名匹配已做容错（`POS 商品管理`/`POS_商品管理`/`POS商品管理` 等价）。

## 执行流程

```powershell
cd ai-test-framework/scripts

# Mock 执行器（测 B/D：Agent/Skill 决策层，不连真实系统，零配置）
python run_test.py --req-type <类型> --dataset ../datasets/<数据集>.yaml --executor mock

# 真实 MCP 执行器（测 C：E2E 层，需系统配置 + .env token）
python run_test.py --req-type <类型> --dataset ../datasets/<数据集>.yaml --executor real

# 统计判定（每条跑 5 次，计算通过率 + 置信区间）
python run_test.py --req-type <类型> --dataset ../datasets/<数据集>.yaml --executor real --runs 5

# 显式指定系统（通常自动从数据集识别，也可手动指定）
python run_test.py --req-type <类型> --dataset ../datasets/<数据集>.yaml --executor real --system <系统名>
```

系统名会自动从数据集「系统」字段读取，无需手动传（避免中文路径乱码）。

## Trace 上报（可视化链路）

加 `--trace` 参数，执行器产出的多层链路（LLM意图→参数→MCP→校验→回答）会自动上报到 trace_platform：

```powershell
python run_test.py --req-type <类型> --dataset ../datasets/<数据集>.yaml --executor real --trace
```

- 上报到 `trace_platform`（默认 `http://127.0.0.1:8000`，可用环境变量 `TRACE_PLATFORM_URL` 覆盖）
- 需先启动 trace_platform：`python -m uvicorn app:app --port 8000`（在 trace_platform/ 目录）
- 服务离线时**跳过上报但提醒**，不影响主流程
- 上报后 trace_id 记录进结果 YAML，报告会生成「Trace 链路」表带查看链接

## 执行器说明

| 执行器 | 用途 | 依赖 |
|---|---|---|
| `mock_executor.py` | Mock（测 Agent/Skill 决策，隔离副作用） | 无 |
| `generic_mcp_executor.py` | 通用真实执行器（A/C/D：直连/对话，E2E，操作后实时校验） | `configs/` + `ability/` 配置 + `.env` token |
| `generic_rag_executor.py` | RAG 执行器（E：检索+生成，按 E 类维度评分） | `configs/` + `ability/` 配置（内置 Mock 检索） |
| `base.py` | 执行器基类（统一接口） | 无 |

- `--executor mock/real/auto` 切换执行器模式
- `auto`：有 token 或 RAG 系统则真实优先，否则纯 Mock（新系统自动降级）
- 按需求类型自动选执行器：A/C/D → MCP，E → RAG

## 评分说明

- **Rubric 5 分制**（5=优秀...1=严重缺陷），每个维度有判定标准
- **统计判定**：每条用例跑 ≥5 次，计算通过率 + Wilson 95% 置信区间
- **操作后实时校验**（E2E）：操作后调真实查询接口拉实时状态核对
- **确定性语义校验**（`semantic_verify.py`）：能力目录「成功标准:语义」的期望自动解析为可校验项（字段/关键词），评分时自动评分（免费/稳定/可解释，via=rule）
- **评分优先级**：规则判定 → 确定性语义校验 → 主观维度用 LLM-as-Judge（`--llm-judge`）；`--llm-detail` 让 LLM 输出详细评分理由（更耗 token）

## 输出

- 执行结果 YAML：`ai-test-framework/results/result_<类型>.yaml`
- 之后交给「报告复盘」skill 生成评估报告
