# AI 能力自动化测试平台

基于《AI 测试方法体系手册》的 AI 测试框架，从需求出发，判断需求类型 → 生成测试数据集 → 执行测试 → Rubric 评分 → 生成评估报告。支持 AI POS、客流统计、AI 摄像头等系统。

---

## 整体架构

```
AI测试/
├── .codebuddy/skills/              # 📁 AI 测试 Skill（3 个）
│   ├── ai-requirement-analysis/    #   Skill① 需求分析：判断需求类型 + 生成数据集
│   ├── ai-test-execution/          #   Skill② 测试执行：执行器 + Rubric 评分
│   └── ai-report-review/           #   Skill③ 报告复盘：评估报告 + 问题定位
│
├── ai-test-framework/              # 📁 AI 测试框架（核心）
│   ├── dimensions/                 #   五类需求维度表（A-E，严格对齐手册）
│   │   ├── A_MCP工具.yaml          #     MCP 工具（9 维：协议契约/工具描述与发现/参数校验/
│   │   │                           #       调用正确性/返回处理/安全与权限/写入安全边界/
│   │   │                           #       性能与资源/异常与容错）
│   │   ├── B_Agent系统.yaml        #     Agent 系统（11 维）
│   │   ├── C_AgentMCP集成.yaml     #     Agent+MCP 集成（21 维，A+B+集成4）
│   │   ├── D_Skill原子能力.yaml    #     Skill 原子能力（6 维）
│   │   └── E_RAG知识库.yaml        #     RAG/知识库（5 维）
│   ├── rubric/                     #   Rubric 评分体系
│   │   ├── rubric.py               #     5 分制评分 + 阈值 + 统计 + 错误归因(数据集/AI系统/环境)
│   │   ├── llm_judge.py            #     LLM-as-Judge 评分器
│   │   ├── semantic_verify.py      #     确定性语义校验器（原语规则打分，不调 LLM）
│   │   └── templates/              #     Rubric 模板（rubric_template.json 含新增维度步骤）
│   ├── executors/                  #   执行器（通用，不绑系统，按需求类型路由）
│   │   ├── base.py                 #     执行器基类 + ExecResult + 数据生命周期契约
│   │   ├── data_lifecycle.py       #     数据生命周期：写前快照/跑后恢复/新建清理/自建自删
│   │   ├── mock_executor.py        #     Mock 执行器（测 Agent/Skill 层，零配置）
│   │   ├── direct_mcp_executor.py  #     A/D 类：纯工具调用（直连 MCP）
│   │   ├── generic_chat_executor.py#     B 类：纯对话 Agent（不连 MCP）
│   │   ├── generic_mcp_executor.py #     C 类：Agent+MCP 集成（配置驱动，测 E2E）
│   │   ├── generic_rag_executor.py #     E 类：RAG 知识库检索
│   │   └── registry.py             #     执行器注册表（按需求类型+系统路由）
│   ├── configs/                    #   系统配置（连接+工具schema，驱动真实执行器）
│   │   ├── RetailPOS数据查询.yaml   #   真实：retailpos（A/C）
│   │   ├── PocketStore.yaml        #   真实：pocketstore（A/C，参数包装=parameters）
│   │   ├── POS_商品管理.yaml        #   接入样板（新增系统复制改）
│   │   ├── POS员工考勤.yaml         #   真实：posattendance（B/C）
│   │   ├── POS数据报表.yaml         #   真实：posreport（A/B/C）
│   │   ├── 小韩面无人值守门禁.yaml    #   真实：unattended（C/D）
│   │   └── 客服知识库.yaml          #   虚构演示（E 链路）
│   ├── scripts/                    #   工具脚本（按流水线阶段排序）
│   │   ├── _01_mcp_setup.py        #   ① 接入 接新MCP：拉schema→契约巡检→写.env→生成configs/<系统>.yaml
│   │   ├── _02_generate_dataset.py #   ② 生成 能力目录+实体清单→数据集 yaml（不用LLM可复现）
│   │   ├── _03_validate_dataset.py #   ③ 检查 硬校验：数据集 vs 能力目录（PASS/FAIL）
│   │   ├── _05_export_to_excel.py  #   ③ 检查 导出 datasets/excel/*.xlsx
│   │   ├── _06_run_test.py         #   ④ 执行 mock/real 执行 + Rubric 评分 + 统计 + 归因透传
│   │   ├── _07_evaluate.py         #   ④ 执行 评分库：维度表加载（load_dimension_tables）
│   │   ├── _08_report.py           #   ⑤ 报告 评估报告生成（含错误归因分组）
│   │   ├── _09_pipeline.py         #   ⑤ 报告 端到端一键（含 --job 快捷别名）
│   │   ├── _10_maintain.py         #   ☆ 维护 status/regen/validate/review/install/export/selftest
│   │   ├── _11_llm_client.py       #   · 公共 LLM 封装（绕代理/JSON容错）
│   │   ├── _12_trace_client.py     #   · 公共 执行结果上报 trace 平台（离线自动跳过）
│   │   └── (工具集，游离)            #   非流水线脚本，手动按需运行：
│   │       pocketstore_mcp.py      #     PocketStore MCP 通用入口（探测/复测首选）
│   │       pos_mcp.py              #     POS MCP 通用调用/查询
│   │       rooster_mcp.py          #     Rooster MCP 查询
│   │       query_pos_realtime.py   #     POS 实时订单查询
│   │       total_report_standard.py#     POS 总报表字段规范（被 rooster_mcp 引用）
│   │       xiaohan_report.py       #     小韩面经营查询
│   ├── ability/                    #   能力目录 + 实体清单（生成源头）
│   │   ├── 能力目录_RetailPOS数据查询.yaml
│   │   ├── 能力目录_PocketStore.yaml
│   │   ├── 能力目录_小韩面无人值守.yaml
│   │   ├── 能力目录_POS商品管理.yaml
│   │   ├── 能力目录_POS员工考勤.yaml
│   │   ├── 能力目录_POS数据报表.yaml
│   │   ├── 能力目录_客服知识库.yaml     #     虚构演示（E 链路）
│   │   ├── 实体清单_RetailPOS数据查询.yaml
│   │   ├── 实体清单_PocketStore.yaml
│   │   ├── 实体清单_POS员工考勤.yaml
│   │   ├── 实体清单_POS数据报表.yaml
│   │   ├── 实体清单_小韩面无人值守.yaml
│   │   ├── 实体清单_小韩面参考.yaml
│   │   └── 商品清单_Test01参考.yaml
│   ├── prompts/                    #   提示词（被测系统人格/回复模板）
│   │   └── ai_pos_reply_prompt.txt #     AI POS 回复提示词
│   ├── datasets/                   #   数据集（结构化生成；install 前自动备份 *.bak_*）
│   │   ├── A_RetailPOS数据查询.yaml #   retailpos A（L1 黄金集）
│   │   ├── A_PocketStore商家经营后台.yaml # pocketstore A
│   │   ├── A_POS 商品管理.yaml      #   早期样例 A
│   │   ├── A_POS员工考勤.yaml       #   早期样例 A
│   │   ├── A_POS数据报表.yaml       #   早期样例 A
│   │   ├── B_POS 商品管理.yaml      #   早期样例 B
│   │   ├── B_POS员工考勤.yaml       #   早期样例 B
│   │   ├── B_POS数据报表.yaml       #   早期样例 B
│   │   ├── C_RetailPOS数据查询.yaml #   retailpos C
│   │   ├── C_PocketStore商家经营后台.yaml # pocketstore C
│   │   ├── C_小韩面无人值守门禁.yaml #   unattended C
│   │   ├── C_POS 商品管理.yaml      #   早期样例 C
│   │   ├── C_POS员工考勤.yaml       #   早期样例 C
│   │   ├── C_POS数据报表.yaml       #   早期样例 C
│   │   ├── D_小韩面无人值守门禁.yaml #   unattended D
│   │   ├── D_POS 商品管理.yaml      #   早期样例 D
│   │   ├── E_客服知识库.yaml        #   虚构演示（E 链路）
│   │   └── excel/                  #   _05_export_to_excel.py 导出的 xlsx
│   ├── results/                    #   执行结果（result_<类型>_<时间戳>.yaml）+ mcp_schemas/
│   ├── report/                     #   评估报告（评估报告_<时间戳>_<类型>_<数据集名>.md）
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
4. **需求类型驱动**：先判断 A/B/C/D/E，再用对应维度表（A=9/B=11/C=21/D=6/E=5）
5. **能力×类型覆盖**：每个能力覆盖正常/边界/异常/对抗/模糊；写操作类能力额外覆盖**影响范围**（见第 11 条）
6. **测试集分层**：L1 黄金集 60% + L2 场景演化 30%（L3 生产回放 10% 暂不接入）
7. **按维度驱动生成**：`_02_generate_dataset.py` 遍历维度表，每个维度按手册「核心测试方法」生成针对性用例（**不用 LLM**，可复现可回溯）。五类各按独立维度表：A→build_a、D→build_d、E→build_e、B/C→build_l1
8. **错误归因升级**：每条失分标注 `attribution`（数据集问题/ AI 系统问题/ 环境问题/ 测试通过），报告分组展示，让开发只看真系统问题、测试修数据缺陷
9. **通用化**：能力目录 + 真实实体清单通过参数注入，真实执行器由系统配置驱动，可复用到任意系统
10. **数据生命周期保护**：真跑写用例前自动快照原值、跑后恢复；新建的自动删除；删除类走「自建自删」不碰真实数据。规则全在配置里，未配置的系统完全旁路
11. **写入安全边界（A 类第 9 维，C 类自动继承）**：专门抓「调用成功但作用范围爆炸」这类缺陷——
    传 1 个 ID 却改了全量数据，且返回 `code=0`，传统「期望拒绝」类断言完全抓不到。
    断言方式改为**两级影响范围对账**：主判据用存在性探针（目标 ID 执行前后是否还在），
    辅判据用快照总数对账（`|Δ| ≤ 传入目标数`）。基线为 0 且无存在性证据时**不判分**，
    避免「Δ 恒为 0 → 恒满分」的假阳性。该类用例自动标 `高危: true`，执行器默认跳过，
    仅在隔离测试环境设 `ALLOW_DESTRUCTIVE=1` 才真跑。

> ⚠️ **维度数以代码为准**：A=9 / B=11 / C=21 / D=6 / E=5，来自
> `_07_evaluate.load_dimension_tables()` 实际加载结果（A 类是手册 8 维 + 框架新增的
> 「写入安全边界」= 9；C = A + B + 集成 4 维去重）。改维度表后请同步核对本表与
> 「五类需求类型」章节，避免两处不一致。

## 五类需求类型

| 类型 | 名称 | 维度数 | 判断依据 |
|---|---|---|---|
| A | MCP 工具 | 9 | 纯 MCP 工具/接口 |
| B | Agent 系统 | 11 | 对话 Agent（无外部工具） |
| C | Agent+MCP 集成 | **21** | **Agent 决策 + 真实 MCP**（如 POS 商品管理）★ 常用 |
| D | Skill 原子能力 | 6 | 某个原子子能力 |
| E | RAG/知识库 | 5 | 文档检索增强生成 |

> 维度数以 `_07_evaluate.load_dimension_tables()` 实际加载结果为准（去重合并后）：
> A=9 / B=11 / C=21 / D=6 / E=5。其中 A 类第 9 维「写入安全边界」为框架在手册 8 维
> 之外新增，专测写操作的**影响范围**，C 类自动继承；玩法见「方法体系核心」第 11 条。
> 改维度表后请同步核对本表。

---

## 脚本速查（按流水线排序）

整体一条链：**①接入 → ②生成 → ③检查 → ④执行 → ⑤报告**；`_10_maintain.py` 是横切维护入口（regen/validate/review/install/export/selftest），公共库被调用、不直接运行。

**两条跑测入口**（按场景选）：
- **`_09_pipeline.py`** —— 跑测主入口：自测 → 生成/复用 → 硬校验(门禁) → 软 review(提醒) → 执行 → 报告，**推荐**
- **`_06_run_test.py`** —— 单点执行：要精细控制参数（`--runs` / `--dims` / `--llm-judge`）时用

| 序号 | 阶段 | 脚本 | 作用 | 什么时候用 |
|---|---|---|---|---|
| ① | 接入 | `_01_mcp_setup.py` | 拉真实 schema → **契约巡检** → 写 `.env` → 生成 `configs/<系统>.yaml` 骨架 | 新增被测 MCP 系统，一条命令接入 |
| ② | 生成 | `_02_generate_dataset.py` | 能力目录+实体清单 → 生成数据集 yaml | 改能力目录/实体清单后重建（种子固定可复现） |
| ②½ | 生成 | `_check_gaps.py` | 检查写用例的数据保护覆盖（有无「无保护」工具） | **生成后、真跑前必做** |
| ②¾ | 生成 | `_selftest_lifecycle.py` | 数据生命周期模块回归自测（35 项） | 改 `data_lifecycle.py` 后必跑 |
| ③ | 检查 | `_03_validate_dataset.py` | 数据集 vs 能力目录 硬校验（PASS/FAIL） | 生成后、提交前必跑 |
| ③ | 检查 | `_05_export_to_excel.py` | 导出 `datasets/excel/*.xlsx` | 人工快速过一遍用例 |
| ④ | 执行 | `_06_run_test.py` | mock/real 执行器跑用例 + Rubric 5 分制评分 + 统计 | 跑测试出评分 |
| ④ | 执行 | `_07_evaluate.py` | 评分库：`load_dimension_tables` 加载维度表（供 _06 调用） | **不是独立入口**，直接运行其 main() 会得到 mock 假评分 |
| ⑤ | 报告 | `_08_report.py` | 结构化报告：得分/通过率/置信区间/问题定位/反哺建议 | 出单次评估报告 |
| ⑤ | 报告 | `_09_pipeline.py` | 端到端一键：自测 → 生成/复用 → 硬校验 → 软 review → 执行 → 报告 | **跑测主入口**，支持 `--job` 别名 |
| ☆ | 维护 | `_10_maintain.py` | `status/regen/validate/review/install/export/selftest` | **日常首选**：看状态、重建、硬校验、软review、装正式版、刷 excel、回归自测 |
| · | 公共 | `_11_llm_client.py` | 公司大模型封装（绕代理/JSON 容错） | 被生成/评分调用 |
| · | 公共 | `_12_trace_client.py` | 上报执行 trace 到 trace_platform（离线自动跳过） | 被执行链调用 |

**日常维护示例**（`_10_maintain.py` 命令全 ASCII、中文内容内置，规避 Windows 命令行中文乱码）：

```powershell
cd ai-test-framework
python scripts/_10_maintain.py status                   # 数据集/备份/待安装/excel 过期一览
python scripts/_10_maintain.py regen    unattended C D  # 重建无人值守 C/D -> *.new.yaml
python scripts/_10_maintain.py validate unattended      # 硬校验 + 报告 results/maintain_report.txt
python scripts/_10_maintain.py review   unattended      # 软review：分布/覆盖/重复 + results/maintain_review.txt
python scripts/_10_maintain.py install                  # 备份旧版 -> 安装所有 *.new.yaml
python scripts/_10_maintain.py export   retailpos       # 刷新 excel
python scripts/_10_maintain.py selftest                 # 框架回归自测（改完代码先跑）
```

系统别名：`retailpos`（A/C）、`unattended`（C/D）、`pocketstore`（A/C）、`posproduct`（A/B/C/D）、`posattendance`（B/C）、`posreport`（A/B/C）；新增系统只需在 `scripts/_10_maintain.py` 顶部 `SYSTEMS` 表加一行，无需新建脚本。

**快捷跑测示例**（`--job` 别名免敲中文路径，中文 argv 在 PowerShell 下会乱码）：

```powershell
cd ai-test-framework
python scripts/_09_pipeline.py --job unattended  --executor real   # D 类小韩面
python scripts/_09_pipeline.py --job retailpos   --executor real   # A 类 RetailPOS
python scripts/_09_pipeline.py --job pocketstore --executor real   # A 类 PocketStore
python scripts/_09_pipeline.py --job pocketstore-c --executor real # C 类 PocketStore
```

> `--job` 别名定义在 `_09_pipeline.py` 的 `JOB_ALIASES`，当前只有
> `retailpos` / `unattended` / `pocketstore` / `pocketstore-c` 四个；
> 其余系统走 `--system <中文系统名>` 或 `_10_maintain.py` 的系统别名。

`--job` 会自动填好 `--req-type / --dataset / --system`，并**带上硬校验 + 软 review 门禁**。

### 产物命名规范（统一带时间戳，不互相覆盖）

| 产物 | 命名 | 说明 |
|---|---|---|
| 执行结果 | `results/result_<类型>_<YYYYMMDD>.yaml` | 同天多次跑自动加 `_HHMMSS` |
| 评估报告 | `report/评估报告_<时间戳>_<类型>_<数据集名>.md` | 一眼看出哪天跑、跑的是哪个数据集 |

> **为什么带时间戳**：不同 runs 采样、不同维度子集、限流后重跑都会产出多份结果，
> 带戳可保留历史、便于对比。**类型以数据集里的「需求类型」为准**——若 `--req-type`
> 与之不符，框架会警告并以数据集为准，避免 result 与 report 命名不一致。

---

## 快速开始（3 个 Skill 流程）

### Skill① 需求分析：判断类型 + 生成数据集（L1/L2 分层）
```powershell
cd ai-test-framework/scripts
# 完整版：指定 需求类型 + 能力目录 + 实体清单
python _02_generate_dataset.py ^
  --req-type <A|B|C|D|E> ^
  --ability ../ability/能力目录_<系统>.yaml ^
  --products ../ability/<实体清单>.yaml ^
  --out ../datasets/<类型>_<系统>.yaml

# 降级版：只传 req-type + system（自动发现能力目录）
python _02_generate_dataset.py --req-type C --system <系统名> --out ../datasets/<类型>_<系统>.yaml
```

### Skill② 测试执行：执行 + Rubric 评分
```powershell
cd ai-test-framework

# ★ 推荐：跑测主入口（自带 硬校验门禁 + 软 review，免敲中文路径）
python scripts/_09_pipeline.py --job <retailpos|unattended|pocketstore|pocketstore-c> --executor real --trace

# 单点执行（要精细控制参数时用）
cd scripts
python _06_run_test.py --req-type C --dataset ../datasets/<数据集>.yaml --executor mock
python _06_run_test.py --req-type C --dataset ../datasets/<数据集>.yaml --executor real --runs 5
```

执行器选择：`mock`（测 Agent/Skill 层，零配置）/ `real`（测 E2E，需 `configs/` + `.env` token）。
系统名自动从数据集识别，也可 `--system <系统名>` 手动指定。

**跑真机前必查「写入安全边界」高危用例**：A 类第 9 维生成的写操作用例会带 `高危: true`，
执行器**默认跳过**。确认是隔离测试环境后，才可这样放开：

```powershell
# 仅在隔离环境！会真实执行批量改/删类用例
$env:ALLOW_DESTRUCTIVE = "1"
python scripts/_09_pipeline.py --job pocketstore --executor real
```

⚠️ **禁止**在含真实数据的生产环境开启；**禁止**用「不存在的 ID」去试探破坏性接口
（过滤参数一旦被服务端忽略，会退化为全量操作——历史上正因此导致「传 1 个 ID 删 983 个」）。
正确做法是「自建 → 操作 → 删自建」，或用真实目标 ID 做执行前后快照对账。

### Skill③ 报告复盘：生成评估报告
```powershell
# 推荐：跑测时加 --report 一步出结果+报告（命名自动带时间戳/类型/数据集名）
cd ai-test-framework/scripts
python _06_run_test.py --req-type C --dataset ../datasets/<数据集>.yaml --executor real --report

# 或单独出报告（--result 指向 results/ 下最新那份 result_<类型>_<时间戳>.yaml）
python _08_report.py --result ../results/result_C_20260927.yaml
```

两种方式产出的报告命名一致：`report/评估报告_<时间戳>_<类型>_<数据集名>.md`。

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

---

## 数据生命周期保护（真跑写用例前必看）

> **解决的问题**：真跑写用例会改动被测系统的真实数据——改价格、删数据、越跑越多的垃圾记录。
> 框架提供三层自动保护，**规则全由配置声明，代码不含任何业务知识**；未配置的系统完全旁路。

### 三层保护

| 层 | 场景 | 行为 | 配置键 |
|---|---|---|---|
| ① 快照还原 | 改值（价格/库存/状态/店名/电话…） | 写前记录原值 → 跑完写回 | `可还原` |
| ② 新建清理 | 新建（费用/邮箱/分类/菜单/商品…） | 记录新实体 ID → 跑完删除 | `新建后清理` |
| ③ 自建自删 | 删除（删商品/删费用…） | 先建临时实体 → 删它，不碰真实数据 | `自建自删` |

### 跑前两步检查

```powershell
cd ai-test-framework

# 1) 看启动日志的「数据保护」行
#    「已启用（可还原 N、新建清理 N、自建自删 N、能力缺口 N）」→ 可安全真跑
#    「未声明「数据生命周期」」→ 写用例会真改数据且不还原

# 2) 确认没有无保护的写工具
python scripts/_check_gaps.py
#    「✅ 所有写工具都有保护」才可跑
```

### 配置示例

```yaml
数据生命周期:
  可还原:
    - 工具: batch_item_price        # 写工具
      快照工具: item_detail         # 用它查原值
      主键参数: merchFoodIds        # 写工具里的目标参数（支持数组）
      主键字段: id                  # 快照返回里的定位字段（常与参数名不同）
      快照字段: [price]
  新建后清理:
    - 工具: add_extra_fee
      删除工具: delete_extra_fee
      删除参数: {merchId: "{merchId}", feeId: "{新实体ID}"}
  自建自删:
    - 删除工具: item_delete
      建工具: item_create
      建参数: {merchId: "{merchId}", name: "_AUTOTEST_DEL"}
      删除主键: itemId
  能力缺口:                          # 能做但无法验证/还原 → 跳过并作为缺陷上报
    - 工具: set_tip_type
      原因: 缺读取当前值的接口，改后无法验证与还原
```

### 关键原则

- **被测系统缺读接口 → 记「能力缺口」，不要硬跑**。如 `set_*` 有写无读，改了既无法验证也无法还原，
  真跑只会污染环境。这本身就是**被测系统缺陷**（API 只有写半身）。
- **宁可跳过，不可误删**。自建自删的临时实体建失败 / 拿不到 ID 时，框架拒绝执行，绝不拿真实 ID 去删。
- **快照目录按运行隔离**（`results/snapshots/<run_id>/`），绝不复用上次快照——
  否则会把已被改脏的值当成原值「还原」成错误值。
- **执行器契约**：`BaseExecutor.get_lifecycle()`。新增会产生写副作用的执行器**必须覆盖它**，
  否则写用例会静默失去保护。纯对话/RAG/Mock 返回 `None` 自动旁路。

---

## 手工探测：走常驻入口，禁止写一次性脚本

> 复测缺陷、临时查数据、验证开发修复情况——**都不准新建 `_tmp_*.py` / `_probe_*.py`**。
> 判定标准：不是「这次算不算接入」，而是「我是不是在为一个具体 MCP 写新文件」。

每个接入的 MCP 有且只有一个常驻入口，长期存在、持续增强：

| 系统 | 常驻入口 |
|---|---|
| PocketStore | `scripts/pocketstore_mcp.py` |
| Rooster POS | `scripts/rooster_mcp.py` |
| POS | `scripts/pos_mcp.py` |

```powershell
cd ai-test-framework

python scripts/pocketstore_mcp.py tools                    # 列工具
python scripts/pocketstore_mcp.py tools menu -v            # 过滤 + 看必填参数
python scripts/pocketstore_mcp.py schema menu_create       # 看单个工具完整入参
python scripts/pocketstore_mcp.py stores                   # 列店铺 + 别名对照

# 读操作（--store 用别名，免敲 uuid）
python scripts/pocketstore_mcp.py call menu_list --store 3 --json pageIndex=1,pageSize=100
python scripts/pocketstore_mcp.py call menu_detail --store 4444 --json menuId=<id>

# 写操作：默认 confirm=false 只预览；加 --go 才真正执行
python scripts/pocketstore_mcp.py call menu_delete --store 3 --json menuId=<id>
python scripts/pocketstore_mcp.py call menu_delete --store 3 --json menuId=<id> --go
```

入口不存在时**先建入口**再用，不要先写临时脚本。缺的通用能力（限流重试、参数外壳、
`--json` 宽松解析、输出格式化）应**补进入口本身**。

入口内置能力（也是新增入口时的实现清单）：

| 能力 | 原因 |
|---|---|
| 参数外壳自动包（读 config 的 `参数包装`） | 每个 MCP 外壳不同（`parameters` / `toolParams`），手工包易错 |
| `merchId` 默认值 + 店铺别名 | 免敲 uuid、避免拿错店铺 ID |
| `confirm` 二段式（默认 `false` / `--go` 才执行） | 写操作零风险预览 |
| **429 写限流自动退避重试** | 多数 MCP 对写操作限流，密集探测必踩 |
| `--json` 宽松解析（裸 key / 单引号 / `key=value`） | PowerShell 会吞掉参数里的双引号 |
| 统一格式化输出（code/msg/items 分开） | 避免整段转义 JSON 刷屏、看漏关键字段 |

---

## 契约巡检（接入新 MCP 时自动跑）

> **解决的问题**：schema 只能看到「工具有哪些参数」，看不到「调下去返回对不对」。

`_01_mcp_setup.py` 接入时自动对每个工具真调一次，检查返回体自洽性：

```powershell
cd ai-test-framework
# 给 2 个以上店铺 ID 才能做跨店对比（强烈建议）
python scripts/_01_mcp_setup.py --name <系统名> --url <MCP端点> --token <TOKEN> \
    --probe-merchants <店铺ID1>,<店铺ID2>

# 纯拉 schema 不巡检
python scripts/_01_mcp_setup.py ... --no-probe
```

产出 `results/mcp_schemas/<系统名>.contract.md` 体检表：

| 检查项 | 含义 |
|---|---|
| `MERCH_ZERO` | 响应归属店铺字段为全零 UUID（未回填真实归属） |
| `MERCH_MISMATCH` | 响应归属店铺与请求不一致（疑似跨租户泄露） |
| `SAME_ACROSS_MERCH` | 已传 merchId 但换店铺返回完全一致（无视过滤） |
| `TENANT_FIXED` | 工具无店铺参数，恒返回 token 绑定店铺的数据 |
| `PREVIEW_IGNORED` | 传 confirm=true 仍返回预览空壳 |
| `ZERO_ID` | 返回全零 UUID 主键（占位/脏数据） |
| `SERVER_CRASH` | 服务端内部错误（非友好校验） |

**安全边界**：写操作一律 `confirm=false` 只预览，绝不真实改删数据。
**参数生成**按 schema 的 type/enum/format 造合法值；实体主键拿不到真实值时**不传**，
避免「拿店铺 ID 当商品 ID」制造假缺陷。

> ⚠️ 巡检只覆盖「返回体自洽性」，**不覆盖语义正确性**。0 异常 ≠ 工具完全正常。
