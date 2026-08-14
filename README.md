# AI 能力自动化测试平台

一套**需求驱动**的 AI 能力测试框架：从需求出发，自动分析评测维度、生成数据集、执行测试、追踪 trace。支持 AI POS、客流统计、AI 摄像头等系统。

---

## 整体架构（两个独立目录 + trace 平台）

```
20260805013102/
├── .codebuddy/skills/
│   ├── dataset-generator/                # 📁 Skill：生成数据集
│   └── test-runner/                      # 📁 Skill：执行测试
│
├── dataset-generator/                    # 📁 生成端（自包含，仅需 LLM）
│   ├── ability/                         #   能力目录（需求预定义，两端共享的单一数据源）
│   │   ├── 能力目录_模板.yaml
│   │   ├── 能力目录_POS数据查询.yaml
│   │   ├── 能力目录_POS商品管理.yaml
│   │   ├── 能力目录_客流统计.yaml
│   │   └── 商品清单_Test01参考.yaml     #   真实商品清单（MCP 拉取，数据集生成基准）
│   ├── scripts/                         #   生成脚本
│   │   ├── pipeline_generate.py         #     LLM 维度推荐 + 生成数据集
│   │   ├── build_dataset.py             #     基于真实商品清单生成数据集（能力×类型全覆盖）
│   │   ├── yaml_to_md.py                #     YAML → MD 评审视图
│   │   ├── check_dataset.py             #     数据集自检（维度/类型/能力×类型组合覆盖）
│   │   ├── llm_client.py                #     公司大模型封装（绕代理/JSON容错）
│   │   └── recommend_dimensions.py      #     维度推荐（关键词版，无 LLM 备用）
│   ├── datasets/                        #   📦 产出物：数据集（数据源 YAML）
│   │   └── POS商品管理_数据集.yaml
│   ├── review/                          #   📦 产出物：评审视图（自动生成 MD）
│   │   └── POS商品管理_数据集.md
│   ├── .env                             #   LLM 配置 + MCP 配置（敏感，勿提交）
│   ├── AI_评测维度规范表.md              #   维度定义（核心）
│   ├── 数据集设计规范.md                 #   数据集设计规范
│   └── 数据集完整性自检Checklist.md      #   自检清单
│
├── test-runner/                         # 📁 执行端（自包含，依赖 trace 平台）
│   ├── executors/                       #   执行器模块
│   │   ├── base.py                      #     执行器基类 + ExecResult
│   │   ├── mock_executor.py             #     Mock 执行器（读能力目录）
│   │   ├── pos_mcp_executor.py          #     真实 POS MCP 执行器（LLM 解析 + 操作后校验）
│   │   └── registry.py                  #     执行器注册表（按能力路由 + mock/real 模式）
│   ├── scripts/                         #   执行脚本
│   │   └── run_dataset.py               #     读数据集 → 调执行器 → 打分 → 上报 trace
│   ├── scorer.py                        #   打分器（0.0~1.0 连续分）
│   └── datasets/                        #   执行用数据集（由生成端产出）
│
└── trace_platform/                      # 📁 trace 平台（存储+展示，被动接收）
    ├── app.py                           #   FastAPI 后端（接收 trace/查询/前端）
    ├── db.py                            #   SQLite 建表
    ├── upload_real_trace.py             #   手动上报 trace 工具
    └── trace_platform.db                #   SQLite 数据库
```

**两端关系**：
- **生成端 `dataset-generator/`**：只产出 `datasets/*.yaml` + `review/*.md`，仅需 LLM，不依赖 trace 平台、不调用执行器。
- **执行端 `test-runner/`**：消费生成端产出的 `datasets/*.yaml`，调用执行器执行、打分、上报 `trace_platform/` 并查看 trace。
- **共享点**：能力目录 `ability/`（单一数据源，生成端维护，执行端执行器引用它）。
- **衔接点**：`datasets/*.yaml`（生成端产出 → 执行端消费）。

**Skill 与功能目录的对应**：
- Skill 统一存放在 `.codebuddy/skills/`（CodeBuddy 标准加载位置），按名字对应到功能目录：
  - `.codebuddy/skills/dataset-generator/` → 操作 `dataset-generator/` 目录
  - `.codebuddy/skills/test-runner/` → 操作 `test-runner/` 目录
- Skill 是编排入口，功能目录是实际代码/数据所在，两者通过名字一一对应。

---

## 快速开始

### 1. 配置 LLM（`dataset-generator/.env`）
```ini
LLM_API_BASE=http://zsgw.sjdistributor.com:40000
LLM_API_KEY=sk-xxx
LLM_MODEL=metis-coder
LLM_NO_PROXY=true
```

### 2. 生成数据集（无需 trace 平台）
```powershell
cd dataset-generator/scripts
python pipeline_generate.py --req "需求描述..." --name "数据集名"
# 或基于真实商品清单：python build_dataset.py
# 产出 dataset-generator/datasets/<名>.yaml + review/<名>.md + 自检报告
```

### 3. 人工评审
打开 `dataset-generator/review/<数据集名>.md`，修正/补充用例。

### 4. 自检覆盖
```powershell
cd dataset-generator/scripts
python check_dataset.py --yaml ../datasets/<数据集名>.yaml
```

**到此数据集已就绪**：你可以只做生成，先停在此处评审。是否执行测试自行决定。

### 5. （执行测试才需要）启动 trace 平台
```powershell
cd trace_platform
python -m uvicorn app:app --port 8000
# 浏览器打开 http://127.0.0.1:8000
```

### 6. 执行测试（读生成端产出的数据集）
```powershell
cd test-runner/scripts
python run_dataset.py --yaml ../../dataset-generator/datasets/<数据集名>.yaml
# 指定执行器模式：--executor real(真实MCP) / mock(模拟)
```

### 7. 看 trace / 打分 / 定位问题
浏览器打开 `http://127.0.0.1:8000`。

> 执行端可选：`executors/` 默认用 Mock 执行器；要接真实 POS MCP，在 `.env` 配置
> `POS_MCP_URL / POS_MCP_TOKEN / POS_MCP_COMPANY_ID / POS_MERCHANT_ID` 后，用 `--executor real`。

---

## 关键脚本归属

| 脚本 | 归属 | 作用 |
|---|---|---|
| `pipeline_generate.py` | 生成端 | LLM 推荐维度 → 分批生成数据集 → 存 YAML → 生成 MD → 自检 |
| `build_dataset.py` | 生成端 | 基于真实商品清单生成数据集（能力×类型全覆盖） |
| `recommend_dimensions.py` | 生成端 | 关键词版维度推荐（无 LLM 时用） |
| `yaml_to_md.py` | 生成端 | YAML → Markdown 表格（评审用） |
| `check_dataset.py` | 生成端 | 自检维度/类型/能力×类型组合覆盖 |
| `llm_client.py` | 生成端 | 公司大模型封装（含绕代理、JSON容错） |
| `run_dataset.py` | 执行端 | 读数据集 → 执行器 → 打分 → 上报平台 |
| `executors/*` | 执行端 | 执行器（Mock / 真实 MCP），把用例翻译成对被测系统的调用 |
| `scorer.py` | 执行端 | 打分器（0.0~1.0 连续分），被 run_dataset 依赖 |
| `trace_platform/app.py` | 平台 | 存储 + 展示 trace |

---

## 核心设计原则

1. **维度驱动**：数据集围绕评测维度设计（规范表 5 大类）
2. **能力标准化**：能力目录独立，数据集 `能力` 引用它（换系统只换目录）
3. **需求预定义 → MCP 校准**：测试前需求定能力，开发完成后真实 MCP 校准
4. **一源两用**：YAML 数据源 + MD 评审视图（自动生成，不重复维护）
5. **打分连续化**：0.0~1.0 连续分（scorer.py）
6. **生成与执行解耦**：物理上分两个独立目录，可独立运行，中间可随时停顿评审
7. **能力×类型全覆盖**：每个能力都要覆盖正常/边界/异常/对抗/模糊，自检强制检查

---

## 真实 MCP 配置

- **URL**: `https://pos-test-mcp.proton-system.com/mcp`
- **鉴权**: Header `Authorization: Bearer <token>` + `CompanyId: <公司ID>`
- **参数**: 必须嵌套 `{"toolParams": {...}}`
- **测试账号**: 公司 `测试公司(9088125566714885)`，店铺 `Test01(9088143804924933)`

> ⚠️ token 属敏感信息，放 `dataset-generator/.env` 的 `POS_MCP_TOKEN`，勿硬编码提交。
