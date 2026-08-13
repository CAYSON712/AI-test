---
name: dataset-generator
description: AI 测试「数据集生成」专用入口。当用户需要从需求出发，自动分析评测维度、设计并生成测试数据集（YAML）、生成评审 MD、做覆盖自检时使用。仅产出数据集与评审文档，不执行任何测试、不依赖 trace 平台。适用于 AI POS、客流统计、AI 摄像头等系统的数据集设计。
---

# 数据集生成

## Overview

本 Skill 把需求转成评审通过的测试数据集，工作目录为 `dataset-generator/`。

- **脚本位置**：`dataset-generator/scripts/`（生成相关）
- **产出物**：`dataset-generator/datasets/<名>.yaml` + `dataset-generator/review/<名>.md`
- **依赖**：仅需 LLM 配置（`dataset-generator/.env`），**不依赖 trace 平台、不调用执行器**
- **独立运行**：可单独使用本 skill 生成数据集；如需执行测试，请用 `test-runner` skill

## 流程

| 步骤 | 做什么 | 脚本/位置 | 依赖 |
|---|---|---|---|
| ① 分析需求 | 理解业务能力、被测系统 | 人工/LLM | — |
| ② 生成数据集 | LLM 推荐维度 + 分批生成数据集 | `dataset-generator/scripts/pipeline_generate.py` | LLM(`.env`) |
| ③ 评审 MD | 检查 LLM 生成的用例 | `dataset-generator/review/<名>.md` | — |
| ④ 自检 | 检查维度/类型/能力覆盖 | `dataset-generator/scripts/check_dataset.py` | — |

## 执行步骤

### Step 1: 确认 LLM 配置
检查 `dataset-generator/.env` 有 LLM_API_BASE / LLM_API_KEY / LLM_MODEL。

### Step 2: 生成数据集
```powershell
cd 20260805013102/dataset-generator/scripts
python pipeline_generate.py --req "需求描述..." --name "数据集名"
# 产出 datasets/<名>.yaml + review/<名>.md + 自检报告
```

### Step 3: 人工 review
打开 `dataset-generator/review/<数据集名>.md`，修正/补充用例。

### Step 4: 自检覆盖
```powershell
cd 20260805013102/dataset-generator/scripts
python check_dataset.py --yaml ../datasets/<数据集名>.yaml
```

**结束**：得到一份评审通过的数据集 YAML。如需执行测试，请用 `test-runner` skill。

## 相关文件

- **能力目录**：`dataset-generator/ability/能力目录_<系统>.yaml`（需求预定义，两端共享的单一数据源；生成的数据集 `能力` 字段引用它，执行端执行器也引用它）
- **维度规范**：`dataset-generator/AI_评测维度规范表.md`
- **设计规范**：`dataset-generator/数据集设计规范.md`
- **自检清单**：`dataset-generator/数据集完整性自检Checklist.md`
- **LLM 封装**：`dataset-generator/scripts/llm_client.py`（绕代理/JSON容错）

## 注意

- LLM 配置在 `dataset-generator/.env`（敏感，勿提交 git）
- 本机有代理劫持，LLM 调用已内部处理（trust_env=False）
