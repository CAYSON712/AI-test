# AI 能力自动化测试平台

基于《AI 测试方法体系手册》的 AI 测试框架，从需求出发，判断需求类型 → 生成测试数据集 → 执行测试 → Rubric 评分 → 生成评估报告。支持 AI POS、客流统计、AI 摄像头等系统。

---

## 整体架构

```
20260805013102/
├── .codebuddy/skills/              # 📁 AI 测试 Skill（3 个）
│   ├── ai-requirement-analysis/    #   Skill① 需求分析：判断需求类型 + 生成数据集
│   ├── ai-test-execution/          #   Skill② 测试执行：执行器 + Rubric 评分
│   └── ai-report-review/           #   Skill③ 报告复盘：评估报告 + 问题定位
│
├── ai-test-framework/              # 📁 AI 测试框架（核心）
│   ├── dimensions/                 #   五类需求维度表（A-E，严格对齐手册）
│   │   ├── A_MCP工具.yaml          #     MCP 工具（8 维）
│   │   ├── B_Agent系统.yaml        #     Agent 系统（11 维）
│   │   ├── C_AgentMCP集成.yaml     #     Agent+MCP 集成（20 维，A+B+集成4）
│   │   ├── D_Skill原子能力.yaml    #     Skill 原子能力（6 维）
│   │   └── E_RAG知识库.yaml        #     RAG/知识库（5 维）
│   ├── rubric/                     #   Rubric 评分体系
│   │   ├── rubric.py               #     5 分制评分 + 阈值 + 统计(通过率/置信区间)
│   │   ├── llm_judge.py            #     LLM-as-Judge 评分器
│   │   └── templates/              #     （预留）Rubric JSON 模板
│   ├── executors/                  #   执行器（通用，不绑系统）
│   │   ├── base.py                 #     执行器基类 + ExecResult
│   │   ├── mock_executor.py        #     Mock 执行器（测 Agent/Skill 层）
│   │   ├── generic_mcp_executor.py #     通用真实执行器（配置驱动，测 E2E）
│   │   └── registry.py             #     执行器注册表（按需求类型+系统路由）
│   ├── configs/                    #   系统配置（连接+工具schema，驱动真实执行器）
│   │   └── POS_商品管理.yaml       #     POS 系统配置样板
│   ├── scripts/                    #   工具脚本
│   │   ├── generate_dataset.py     #     结构化生成数据集（维度表+能力+实体+规则模板）
│   │   ├── run_test.py             #     测试执行 + Rubric 评分 + 统计
│   │   ├── evaluate.py             #     评估入口
│   │   ├── report.py               #     评估报告生成
│   │   └── llm_client.py           #     LLM 封装（绕代理/JSON容错）
│   ├── ability/                    #   能力目录 + 真实实体清单（按系统）
│   │   ├── 能力目录_POS商品管理.yaml
│   │   ├── 能力目录_POS数据查询.yaml
│   │   └── 商品清单_Test01参考.yaml
│   ├── datasets/                   #   数据集（结构化生成）
│   ├── results/                    #   执行结果
│   ├── report/                     #   评估报告
│   ├── docs/手册方法论落地.md       #   手册落地说明
│   └── .env                        #   LLM + MCP 配置（敏感，勿提交）
│
└── trace_platform/                 # 📁 trace 平台（存储+展示）
    ├── app.py                      #   FastAPI 后端
    ├── db.py                       #   SQLite 建表
    └── trace_platform.db
```

## 方法体系核心（手册）

1. **AI 测试 = 统计学测试**：每条用例跑 ≥5 次，用通过率 + 置信区间，而非单次 pass/fail
2. **Rubric 量化评分**：5 分制（优秀/良好/可接受/一般缺陷/严重缺陷）
3. **LLM-as-Judge**：主观维度用 LLM 当裁判打分
4. **需求类型驱动**：先判断 A/B/C/D/E，再用对应维度表
5. **能力×类型覆盖**：每个能力覆盖正常/边界/异常/对抗/模糊
6. **测试集分层**：L1 黄金集 60% + L2 场景演化 30%（L3 生产回放 10% 暂不接入）
7. **结构化生成**：L1 用规则模板、L2 用确定性变异（**不用 LLM**），可复现可回溯
8. **通用化**：能力目录 + 真实实体清单通过参数注入，真实执行器由系统配置驱动，可复用到任意系统

## 五类需求类型

| 类型 | 名称 | 维度数 | 判断依据 |
|---|---|---|---|
| A | MCP 工具 | 8 | 纯 MCP 工具/接口 |
| B | Agent 系统 | 11 | 对话 Agent（无外部工具） |
| C | Agent+MCP 集成 | 20 | **Agent 决策 + 真实 MCP**（如 POS 商品管理）★ 常用 |
| D | Skill 原子能力 | 6 | 某个原子子能力 |
| E | RAG/知识库 | 5 | 文档检索增强生成 |

---

## 快速开始（3 个 Skill 流程）

### Skill① 需求分析：判断类型 + 生成数据集（L1/L2 分层）
```powershell
cd ai-test-framework/scripts
# 完整版：指定 需求类型 + 能力目录 + 实体清单
python generate_dataset.py ^
  --req-type <A|B|C|D|E> ^
  --ability ../ability/能力目录_<系统>.yaml ^
  --products ../ability/<实体清单>.yaml ^
  --out ../datasets/<类型>_<系统>.yaml

# 降级版：只传 req-type + system（自动发现能力目录）
python generate_dataset.py --req-type C --system <系统名> --out ../datasets/<类型>_<系统>.yaml
```

### Skill② 测试执行：执行 + Rubric 评分
```powershell
cd ai-test-framework/scripts
# Mock（测 Agent/Skill 层，零配置）
python run_test.py --req-type C --dataset ../datasets/<数据集>.yaml --executor mock
# 真实 MCP（测 E2E，需 configs/ 系统配置 + .env 配 token）
python run_test.py --req-type C --dataset ../datasets/<数据集>.yaml --executor real --runs 5
# 系统名自动从数据集识别，也可手动指定
python run_test.py --req-type C --dataset ../datasets/<数据集>.yaml --executor real --system <系统名>
```

### Skill③ 报告复盘：生成评估报告
```powershell
cd ai-test-framework/scripts
python report.py --result ../results/result_<类型>.yaml --out ../report/<报告名>.md
```

---

## 系统配置与连接

**通用化接入任何系统需要两份配置 + 一份 env 密钥**：

| 配置 | 位置 | 内容 |
|---|---|---|
| 系统配置 | `configs/<系统>.yaml` | 连接（URL/token env 名）+ MCP 工具 schema + verify 规则 |
| 能力目录 | `ability/能力目录_<系统>.yaml` | 能力→工具映射 + verify 字段 |
| 敏感密钥 | `.env` | token / 公司ID / 店铺ID（`*.env` 已被 `.gitignore` 保护） |

**新增系统流程**：复制 `configs/POS_商品管理.yaml` → 填新系统连接 + 工具 → 准备对应能力目录 → 即可跑 `real`。系统名匹配已容错（`POS 商品管理`/`POS_商品管理` 等价）。

> ⚠️ **占位样例**：`configs/客服知识库.yaml`、`ability/能力目录_客服知识库.yaml`、`datasets/E_客服知识库.yaml` 为验证 E 类链路的**虚构演示**，非真实系统。真实接入请按被测系统改写。

> ⚠️ token 属敏感信息，放 `.env`，勿硬编码提交。系统名自动从数据集读取，避免命令行中文乱码。
