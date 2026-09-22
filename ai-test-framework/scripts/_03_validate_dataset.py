# -*- coding: utf-8 -*-
"""数据集自检脚本：对生成的数据集做结构化校验 + 覆盖率总览。

用法（PowerShell）:
    python scripts/_03_validate_dataset.py --dataset datasets/A_RetailPOS数据查询.yaml --ability ability/能力目录_RetailPOS数据查询.yaml
    python scripts/_03_validate_dataset.py --dataset datasets/C_RetailPOS数据查询.yaml --ability ability/能力目录_RetailPOS数据查询.yaml

检查项:
  [F] 致命：文件无法解析 / 结构缺失 / 用例数不一致
  [E] 错误：字段缺失、非法值、ID 不连续、维度/能力/工具不合法、block 语义矛盾、
             意图-参数矛盾（期望正常执行却传非法参数）、sample_extra 违反规则
             （注意：期望被拒绝 + 非法参数 = 正确的异常用例，不会误报）
  [W] 警告：覆盖偏少、重复输入、实体名可疑、分层比例偏差
  [I] 信息：各维度/能力/层用例数分布（帮快速概览全量数据）
"""
import argparse
import glob
import os
import re
import sys
from collections import Counter, defaultdict

try:
    import yaml
except ImportError:
    sys.exit("缺少 PyYAML，请先 pip install pyyaml")

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
VALID_LAYERS = {"L1", "L2"}

NEGATIVE_WORDS = ["拒绝", "失败", "非法", "错误", "越权", "无效", "缺失",
                  "无数据", "暂无", "不允许", "不可", "重试", "容错", "崩溃"]

# 合法标签集合（生成器 _make_b_label 等产出的已知标签；未知标签仅告警）
KNOWN_TAGS = {
    "正常", "异常", "对抗", "边界", "越权", "多轮", "模糊", "多意图", "指代",
    "多工具", "编排", "组合", "多步", "状态跟踪", "覆盖", "实体替换", "数值变异",
    "表达改写", "注入对抗", "参数缺失", "畸形", "容错", "检索", "忠实", "时效",
    "竞态", "工具选择", "顺序", "回退", "参数错误", "契约",
    "参数注入", "类型错配", "工具错配",
}

# A/D 类返回结构化 dict，期望里允许的 semantic 键
# C/B 类为对话式，semantic 应为 contains + any_of
REQ_TYPES = {"A", "B", "C", "D", "E"}


# ---------------------------------------------------------------------------
# 加载
# ---------------------------------------------------------------------------
def load_yaml(path):
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_ability(ability_path):
    """返回 (能力名集合, 工具名集合, 能力名->工具名映射)。"""
    doc = load_yaml(ability_path)
    names, tools, tool_of = set(), set(), {}
    for group in doc.get("能力分组") or []:
        for cap in group.get("能力列表") or []:
            n = cap.get("能力")
            if not n:
                continue
            names.add(n)
            t = cap.get("工具")
            if t:
                tools.add(t)
                tool_of[n] = t
    return names, tools, tool_of


def load_dims(req_type):
    """返回某需求类型合法维度名集合。
    C 类 = A8 维 + B11 维 + 集成 4 维，去重合并。
    """
    dims = set()
    if req_type == "A":
        doc = load_yaml(DIM_A)
        dims |= {d["维度"] for d in doc.get("维度列表") or []}
    elif req_type == "C":
        a = load_yaml(DIM_A).get("维度列表") or []
        b = load_yaml(DIM_B).get("维度列表") or []
        c = load_yaml(DIM_C).get("集成特有维度") or []
        dims |= {d["维度"] for d in a + b + c}
    else:
        doc = load_yaml({"B": DIM_B, "D": DIM_D, "E": DIM_E}[req_type])
        dims |= {d["维度"] for d in doc.get("维度列表") or []}
    return dims


DIM_A = None
DIM_B = None
DIM_C = None
DIM_D = None
DIM_E = None


# ---------------------------------------------------------------------------
# 检查器
# ---------------------------------------------------------------------------
class Checker:
    def __init__(self, dataset, ability_path, products_path=None, verbose=False):
        self.dataset = load_yaml(dataset)
        self.ability_path = ability_path
        self.req_type = self.dataset.get("需求类型", "?")
        self.cases = self.dataset.get("用例列表") or []
        self.verbose = verbose
        self.f, self.e, self.w, self.i = [], [], [], []

    # -- 输出 --
    def fail(self, msg):
        self.f.append(msg)

    def error(self, msg):
        self.e.append(msg)

    def warn(self, msg):
        self.w.append(msg)

    def info(self, msg):
        self.i.append(msg)

    # -- 检查 --
    def check_top(self):
        for key in ("系统", "需求类型", "分层说明", "用例数", "用例列表"):
            if key not in self.dataset:
                self.fail(f"顶层字段缺失: {key}")
        declared = self.dataset.get("用例数")
        if declared is not None and declared != len(self.cases):
            self.error(f"用例数不一致: 声明 {declared} != 实际 {len(self.cases)}")
        if self.req_type not in REQ_TYPES:
            self.error(f"需求类型非法: {self.req_type}")

    def check_ids(self):
        ids = [c.get("用例ID") for c in self.cases]
        if len(set(ids)) != len(ids):
            dup = [x for x, n in Counter(ids).items() if n > 1]
            self.error(f"用例ID 重复: {dup}")
        for i, cid in enumerate(ids, 1):
            if not cid:
                self.error(f"第 {i} 条用例缺少用例ID")
                continue
            m = re.fullmatch(rf"{self.req_type}-L[12]-\d{{3}}", str(cid))
            if not m:
                self.error(f"用例ID 格式非法: {cid}（应为 {self.req_type}-L1/L2-3位数字）")
                continue
            seq = int(str(cid).rsplit("-", 1)[1])
            if seq != i:
                self.warn(f"用例ID 序号不连续: {cid} 出现在第 {i} 位")

    def check_fields(self, names, tools, tool_of):
        for i, c in enumerate(self.cases, 1):
            uid = c.get("用例ID", f"#{i}")
            for key in ("维度", "能力", "层", "输入", "期望", "标签"):
                if key not in c:
                    self.error(f"{uid}: 缺少字段「{key}」")
            layer = c.get("层")
            if layer and layer not in VALID_LAYERS:
                self.error(f"{uid}: 层非法 {layer}（应为 L1/L2）")
            dim = c.get("维度")
            if dim and dim not in self.valid_dims:
                self.error(f"{uid}: 维度「{dim}」不在维度表内")
            cap = c.get("能力")
            if cap and cap not in names:
                self.error(f"{uid}: 能力「{cap}」不在能力目录内")
            expect = c.get("期望") or {}
            if self.req_type == "E":
                # E 类（RAG）：期望核心是 expect_docs/expect_answer，params/block 为通用字段
                if not isinstance(expect, dict) or (
                        "expect_docs" not in expect and "expect_answer" not in expect):
                    self.error(f"{uid}: E 类期望必须含 expect_docs 或 expect_answer")
                if not isinstance(expect, dict) or "params" not in expect:
                    self.error(f"{uid}: 期望缺少「params」")
                if not isinstance(expect, dict) or "block" not in expect:
                    self.error(f"{uid}: 期望缺少「block」")
            else:
                for key in ("intent", "params", "output", "block"):
                    if key not in expect:
                        self.error(f"{uid}: 期望缺少「{key}」")
            sem = (expect or {}).get("semantic") or {}
            # 输出内容类维度才需要 semantic（rubric 只在这些维度消费它）：
            # 其余维度（调用正确性/工具描述/性能/意图识别等）semantic 不参与评分，
            # 缺失不该告警——此前对所有维度一刀切告警，产生大量误报噪音。
            output_dims = ("返回处理", "语义输出", "输出格式", "语义正确性", "回答正确性")
            if not sem:
                if dim in output_dims and self.req_type != "E":
                    self.warn(f"{uid}: 输出类维度「{dim}」期望缺少 semantic 字段（评分器判不了，只能走 LLM/默认3分）")
            else:
                if self.req_type in ("B", "C"):
                    if "contains" not in sem or not sem.get("contains"):
                        self.error(f"{uid}: B/C 类 semantic 必须含非空 contains")
                    if not sem.get("any_of"):
                        self.warn(f"{uid}: B/C 类 semantic 建议 any_of=true")
                    if "fields" in sem:
                        self.error(f"{uid}: B/C 类不应保留 fields（应转 contains）")
                elif self.req_type == "E":
                    if "contains" not in sem or not sem.get("contains"):
                        self.warn(f"{uid}: E 类 semantic 建议含 contains（答案关键词）")
                else:
                    block = expect.get("block")
                    # 容错/宽容语义用 contains 关键词（any_of）校验即可；
                    # fields 结构校验仅对「能预知响应结构」的用例有意义。
                    if ("fields" not in sem and block is not True
                            and not sem.get("contains")):
                        self.warn(f"{uid}: A/D 类 semantic 建议含 fields 结构校验")
            tags = c.get("标签") or []
            if not tags:
                self.warn(f"{uid}: 标签为空")
            for t in tags:
                if t not in KNOWN_TAGS:
                    self.warn(f"{uid}: 未知标签「{t}」（如非手写请忽略）")

            # A 类输入结构：tool_name / tool_params
            if self.req_type == "A":
                inp = c.get("输入") or {}
                if not isinstance(inp, dict) or "tool_name" not in inp:
                    self.error(f"{uid}: A 类输入必须含 tool_name")
                # 「工具错配」负向用例故意把 tool_name 写错（期望拒绝触发），跳过存在性比对
                if isinstance(inp, dict) and inp.get("tool_name") and tool_of.get(cap) \
                        and inp["tool_name"] != tool_of[cap] and "工具错配" not in tags:
                    self.error(f"{uid}: 工具不匹配 输入 {inp['tool_name']} != 能力目录 {tool_of[cap]}")
                if "tool_params" not in (inp or {}):
                    self.error(f"{uid}: A 类输入必须含 tool_params")

            # A 类参数一致性：期望.params 应等于 输入.tool_params
            # 注意：参数校验/边界/缺失/注入 类用例故意构造非法输入（期望为合法参数），跳过
            if self.req_type == "A" and isinstance(c.get("输入"), dict):
                dim = c.get("维度") or ""
                tags = c.get("标签") or []
                if dim != "参数校验" and not any(t in tags for t in
                                                 ("边界", "参数缺失", "注入对抗", "畸形", "数值变异")):
                    tp = (c.get("输入") or {}).get("tool_params") or {}
                    exp_params = expect.get("params") or {}
                    if tp and exp_params and tp != exp_params:
                        self.warn(f"{uid}: 期望.params 与 输入.tool_params 不一致")

    def check_block_semantic(self):
        for i, c in enumerate(self.cases, 1):
            uid = c.get("用例ID", f"#{i}")
            expect = c.get("期望") or {}
            block = expect.get("block")
            output = str(expect.get("output", ""))
            intent = str(expect.get("intent", ""))
            tags = c.get("标签") or []
            if block is True:
                neg_hit = any(w in output for w in NEGATIVE_WORDS) or \
                          any(w in intent for w in NEGATIVE_WORDS)
                if not neg_hit:
                    self.error(f"{uid}: block=true 但 output/intent 无负向语义词（{output}）")
                if "正常" in tags:
                    self.error(f"{uid}: block=true 但标签含「正常」，自相矛盾")
            elif block is False:
                if "正常" not in tags and not any(t in tags for t in
                                                  ("多轮", "模糊", "多意图", "指代",
                                                   "异常", "容错", "多工具", "编排",
                                                   "组合", "覆盖", "状态跟踪", "多步", "实体替换",
                                                   "数值变异", "表达改写", "时效", "忠实",
                                                   "检索", "竞态", "工具选择", "顺序",
                                                   "回退", "参数错误", "畸形")):
                    self.warn(f"{uid}: block=false 且标签既非正常也非典型正向量，请确认")

    # ---- 意图-参数一致性（防生成器「本意合法、实际产出非法」）----
    # 背景事故（2026-09-17）：实体清单为「任意实体回退都能填出合法参数」把所有
    #   字段冗余挂到每个实体上，生成器按参数名跨域取错值——商品工具的 type 取到
    #   了支付方式 "CreditCard"。这类用例「期望正常执行（block=false）却传非法参数」，
    #   意图与参数自相矛盾：既没测到异常处理（期望不是拒绝），也没测到正常调用
    #   （参数非法），438 条被判失分，污染了整套结果。
    #
    # ⚠ 关键区分（勿混淆）：
    #   - 期望 block=true + 非法参数  → ✅ 正确的异常场景用例，放行
    #   - 期望 block=false + 合法参数 → ✅ 正确的正常场景用例，放行
    #   - 期望 block=false + 非法参数 → ❌ 意图与参数矛盾（生成器 bug），报警
    #   - 期望 block=true  + 合法参数 → ⚠️ 期望可能立错，提示复核
    # 绝不能按「参数是否合法」一刀切去拦——那会把大量有意的异常用例误杀。
    # 参数级枚举白名单（与生成器 _PARAM_ENUMS 保持一致；此处用于「反向判定」）
    PARAM_ENUMS = {
        "type": {"product": ("Normal", "Preference", "RechargeBenefit",
                             "PointExchange", "MemberBenefit"),
                 "menu_report": ("Menu", "Product", "Category")},
        "status": ("Selling", "Off", "Active", "InActive"),
        "orderstatus": ("Progressing", "Completed", "Cancelled", "BeMerged",
                        "Returned", "Pending", "Preparing", "Ready",
                        "AwaitingCancel", "AwaitingRefund"),
        "ordertype": ("DineIn", "Pickup", "ToGo", "Delivery",
                      "OnlineDineIn", "OnlinePickup"),
        "ordersourcetype": ("AutoMart", "PocketStore", "Deliverect",
                            "AiPhoneOrder", "ScanCodeToOrder",
                            "MarketOnlineOrder", "SupplyChainCancelOrder", "Kiosk"),
        "paymenttype": ("Cash", "CreditCard", "DebitCard", "Other"),
        "paymentstatus": ("Unpaid", "PartiallyPaid", "FullyPaid", "Captured"),
        "paymentterminaltype": ("Physical", "Virtual", "None"),
        "sortfield": ("Date", "Amount", "Quantity"),
        "sortdirection": ("Ascending", "Descending"),
        "resultmode": ("Count",),
        "relativeperiod": ("Today", "Yesterday", "ThisWeek", "LastWeek",
                           "ThisMonth", "LastMonth"),
        "operationsources": ("UnattendedModeEnabled", "UnattendedModeDisabled",
                             "SystemAccount", "Member", "SystemAutoClose",
                             "CompanyAccount"),
    }
    # 布尔型参数：值必须是 bool（否则意图为「正常执行」时即为矛盾）
    PARAM_BOOL = {"includecategory", "includespecificationgroup", "includemenu",
                  "hasmember", "isenabled"}

    def _illegal_params(self, tool_name, params, dim, tags):
        """返回该调用的「非法参数」说明列表（空 = 全部合法）。

        只判定【值域/类型是否合法】，不判定业务是否存在——
        故本函数用于识别「期望正常执行却传了非法参数」的矛盾用例。
        """
        bad = []
        if not isinstance(params, dict):
            return bad
        tool = str(tool_name or "").lower()
        for k, v in params.items():
            kl = str(k).lower()
            # 1) 布尔型参数：字符串/数字都非法（'1' / 0 / 'abc' 均非 bool）
            if kl in self.PARAM_BOOL:
                if not isinstance(v, bool):
                    bad.append(f"{k}={v!r} 非布尔值")
                continue
            # 2) 枚举型参数：值必须在白名单内
            spec = self.PARAM_ENUMS.get(kl)
            if spec is None:
                continue
            if kl == "type":
                if "menu_product_category" in tool:
                    allowed = spec["menu_report"]
                else:
                    allowed = spec["product"]
            else:
                allowed = spec
            vals = v if isinstance(v, list) else [v]
            for x in vals:
                if not isinstance(x, str) or x not in allowed:
                    bad.append(f"{k}={x!r} 不在合法枚举 {list(allowed)[:4]}...")
        return bad

    # ID 类参数名（含 id/ids/_id 结尾），值形态：正整数构成的字符串
    _ID_RE = re.compile(r"\d+$")

    def _id_params_illegal(self, params):
        """返回「ID 类参数值形态明显非法」的说明列表。

        ⚠ 必须覆盖 ID 类，否则「越权/不存在的店铺 ID」这类正确异常用例会被
        误判成「期望被拒绝但参数合法」。实测：A-L1-003 传 merchantIds=['0']、
        A-L2-004 传 ['99999999999999']，都是刻意的非法 ID，期望被拒绝是正确的。
        判定标准（只认「明显非法」，业务上不存在不算非法）：
          - 空串
          - 非正整数：'0' / '-1' / 'abc' / '1 OR 1=1'
        """
        bad = []
        if not isinstance(params, dict):
            return bad
        for k, v in params.items():
            kl = str(k).lower()
            if not (kl.endswith("id") or kl.endswith("ids")):
                continue
            if kl in self.PARAM_BOOL:
                continue
            vals = v if isinstance(v, list) else [v]
            for x in vals:
                if not isinstance(x, str):
                    bad.append(f"{k}={x!r} 非字符串")
                    continue
                s = x.strip()
                if s == "":
                    bad.append(f"{k} 为空串")
                elif not self._ID_RE.match(s) or int(s) <= 0:
                    bad.append(f"{k}={x!r} 非正整数 ID")
        return bad

    def check_intent_param_consistency(self):
        """意图-参数一致性：拦住「期望正常执行却传非法参数」的矛盾用例。"""
        for i, c in enumerate(self.cases, 1):
            uid = c.get("用例ID", f"#{i}")
            expect = c.get("期望") or {}
            if not isinstance(expect, dict):
                continue
            block = expect.get("block")
            inp = c.get("输入") or {}
            if not isinstance(inp, dict):
                continue
            tool = inp.get("tool_name")
            tp = inp.get("tool_params") or {}
            if not tool or not isinstance(tp, dict):
                continue
            dim = c.get("维度") or ""
            tags = c.get("标签") or []

            # ⚠ 容错/异常维度：用例「故意」传非法输入，期望是「不崩溃 + 被友好处理」，
            #   此时 block=false 是刻意的（不要求拒绝，只要求不崩）。这类必须放行，
            #   否则会把有意的鲁棒性测试全打成矛盾用例。
            #   实测误报源：A-L1-116「容错」传 merchantIds=['0'] + unknown_param_malformed；
            #   D-L1-004「容错」传 _invalid:'' —— 都是正确的容错用例。
            if any(k in dim for k in ("容错", "异常", "鲁棒", "健壮")) or \
                    any(t in tags for t in ("容错", "鲁棒", "畸形", "参数缺失",
                                            "注入对抗", "数值变异", "类型错配")):
                continue
            # 生成器专为「容错/畸形」注入的占位参数名，本身即非业务参数，跳过
            if any(str(k).startswith("_") or "unknown_param" in str(k)
                   or "malformed" in str(k) for k in tp):
                continue

            bad = self._illegal_params(tool, tp, dim, tags)
            bad += self._id_params_illegal(tp)

            # 情形 A：期望正常执行，却传了非法参数 → 意图与参数矛盾（必报错误）
            if block is False and bad:
                self.error(
                    f"{uid}: 意图-参数矛盾 —— 期望正常执行(block=false)但参数非法: "
                    f"{'; '.join(bad[:3])}（疑生成器跨域取错值）")
            # 情形 B：期望被拒绝，但参数【完全合法】→ 期望可能立错（告警复核）
            # 仅当参数非空且所有参数都通过合法性检查时才提示，避免噪音。
            elif block is True and not bad and tp:
                self.warn(
                    f"{uid}: 期望被拒绝(block=true)但参数值域/形态均合法，"
                    f"请确认该期望是否有依据（若靠业务不存在拒绝，可忽略）")

    def check_sample_extra(self):
        for i, c in enumerate(self.cases, 1):
            uid = c.get("用例ID", f"#{i}")
            dim = c.get("维度") or ""
            tags = c.get("标签") or []
            se = c.get("sample_extra")
            if se is None:
                continue
            if not isinstance(se, int) or se < 1:
                self.error(f"{uid}: sample_extra 非法值 {se}")
                continue
            if any(k in dim for k in ("安全", "鲁棒", "对抗")) or "对抗" in tags:
                if se != 5:
                    self.error(f"{uid}: 安全/鲁棒/对抗 维度 sample_extra 应为 5，实际 {se}")
            elif dim in ("参数端到端准确率", "参数生成"):
                if se != 3:
                    self.warn(f"{uid}: 参数端到端/参数生成 sample_extra 通常为 3，实际 {se}")

    # ---- configs/ 与 ability/ 一致性（新增系统时最易出错的一环）----
    # 背景（2026-09-22 通用化）：参数取值域已从 _02 硬编码迁到 configs/<系统>.yaml，
    #   生成器按「能力目录的工具名」去 config 查 param_enums/param_bools。
    #   若两者对不上（工具名拼写不同、config 缺该工具），收敛会静默失效——
    #   不报错，只是非法参数又流回数据集。故此处做一致性前置检查。
    def check_config_consistency(self, names, tools, tool_of):
        cfg_dir = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "configs")
        if not os.path.isdir(cfg_dir):
            self.warn("未找到 configs/ 目录，跳过配置一致性检查")
            return

        sys_name = self.dataset.get("系统") or ""

        def norm(s):
            return str(s or "").replace("_", "").replace(" ", "").replace("-", "").lower()

        # 定位本系统的配置（按「系统」字段 → 文件名包含 → 需求类型）
        cfg_path, cfg = None, {}
        cands = []
        for f in sorted(os.listdir(cfg_dir)):
            if not f.endswith(".yaml"):
                continue
            p = os.path.join(cfg_dir, f)
            try:
                d = load_yaml(p) or {}
            except Exception:
                continue
            if norm(d.get("系统")) == norm(sys_name):
                cfg_path, cfg = p, d
                break
            fn = norm(os.path.splitext(f)[0])
            if fn and sys_name and (fn in norm(sys_name) or norm(sys_name) in fn):
                cands.append((len(os.path.splitext(f)[0]), p, d))
        if not cfg_path and cands:
            _, cfg_path, cfg = max(cands)

        if not cfg_path:
            self.warn(f"未找到系统「{sys_name}」对应的 configs/*.yaml"
                      f"（参数取值域无法校验）")
            return

        self.info(f"  配置文件: {os.path.basename(cfg_path)}")
        declared = {t.get("name") for t in (cfg.get("mcp_tools") or [])
                    if isinstance(t, dict) and t.get("name")}

        # 1) 能力目录引用的工具必须在 configs 声明
        missing = sorted({tool_of[n] for n in names if tool_of.get(n)}
                         - declared) if declared else []
        for t in missing:
            self.error(f"能力目录引用了 configs 未声明的工具「{t}」"
                       f"（{os.path.basename(cfg_path)}）——"
                       f"生成器查不到该工具的取值域，参数收敛会静默失效")

        # 2) configs 声明的工具是否都被能力目录用到（信息级，可能是保留工具）
        unused = sorted(declared - set(tools)) if declared else []
        if unused:
            self.info(f"  configs 声明但能力目录未使用: {', '.join(unused[:6])}"
                      f"{' …' if len(unused) > 6 else ''}")

        # 3) param_enums 的 tools 限定必须指向已声明工具
        pe = cfg.get("param_enums") or {}
        for pname, spec in pe.items():
            rules = spec if isinstance(spec, list) and spec and isinstance(spec[0], dict) else []
            for r in rules:
                for t in (r.get("tools") or []):
                    if declared and t not in declared:
                        self.error(f"param_enums.{pname} 的 tools 指定了"
                                   f"未声明工具「{t}」")

        # 4) 取值域是否为空（配了但空 → 无声失效）
        if not pe and not (cfg.get("param_bools") or []):
            self.warn(f"{os.path.basename(cfg_path)} 未声明 param_enums/param_bools"
                      f"——枚举参数不会做跨域收敛（新增系统建议补上）")

    def check_dup(self):
        # 判定键加入「维度」：不同维度考察点不同，属有效覆盖而非重复；
        # 同维度内出现相同 能力+输入 仍视为真重复告警。
        seen = defaultdict(list)
        for c in self.cases:
            key = (c.get("能力"), str(c.get("输入")), c.get("维度"))
            seen[key].append(c.get("用例ID"))
        for key, ids in seen.items():
            if len(ids) > 1:
                self.warn(f"疑似重复用例（能力+输入+维度相同）: {ids}")

    def check_coverage(self, names):
        dim_cnt = Counter(c.get("维度") for c in self.cases)
        cap_cnt = Counter(c.get("能力") for c in self.cases)
        layer_cnt = Counter(c.get("层") for c in self.cases)

        # 维度覆盖
        missing = self.valid_dims - set(dim_cnt)
        if missing:
            self.warn(f"未覆盖维度: {sorted(missing)}")
        for dim in sorted(dim_cnt):
            self.info(f"  维度[{dim}]: {dim_cnt[dim]} 条")
        # 能力覆盖
        missing_cap = names - set(cap_cnt)
        if missing_cap:
            self.warn(f"未覆盖能力: {sorted(missing_cap)}")
        for cap in sorted(cap_cnt):
            self.info(f"  能力[{cap}]: {cap_cnt[cap]} 条")
        # 分层比例（L1≈60%、L2≈30%）
        total = len(self.cases) or 1
        for layer in sorted(layer_cnt):
            pct = layer_cnt[layer] / total * 100
            self.info(f"  层[{layer}]: {layer_cnt[layer]} 条 ({pct:.1f}%)")
        if layer_cnt.get("L1"):
            l1_pct = layer_cnt["L1"] / total * 100
            if not (50 <= l1_pct <= 75):
                self.warn(f"L1 占比 {l1_pct:.1f}%（期望约 60%±15%）")
        if layer_cnt.get("L2"):
            l2_pct = layer_cnt["L2"] / total * 100
            if self.req_type == "A":
                if l2_pct > 15:
                    self.warn(f"A 类 L2 占比 {l2_pct:.1f}%（接口层以 L1 为主）")
            elif not (20 <= l2_pct <= 45):
                self.warn(f"L2 占比 {l2_pct:.1f}%（期望约 30%±15%）")

    def check_entities(self, products_path, names=None):
        if not products_path:
            return
        doc = load_yaml(products_path)
        entities = set()
        names = set(names or ())
        for key in ("实体清单", "门店清单", "商品清单", "会员清单"):
            for e in doc.get(key) or []:
                entities.add(e.get("名称"))
                for fld in ("id", "storeId", "productId", "memberId", "merchantId", "companyId"):
                    if e.get(fld):
                        entities.add(str(e[fld]))
        for i, c in enumerate(self.cases, 1):
            uid = c.get("用例ID", f"#{i}")
            inp = c.get("输入")
            if not isinstance(inp, str):
                continue
            # 挑出看起来像中文实体名的片段（4字以上，含 店/面/饭/鸡/糕/会员 等）
            cands = re.findall(r"[\u4e00-\u9fa5]{3,}", inp)
            for cand in cands:
                if any(k in cand for k in ("店", "面", "饭", "鸡", "糕", "会员", "菜")):
                    if "不存在" in cand:
                        continue
                    if cand in entities:
                        continue
                    # 能力名片段放行（如「会员注册时间」⊂「查询会员注册时间」，非实体名）
                    if any(cand in n for n in names):
                        continue
                    # 简称放行：清单中某实体名包含该候选（如 "中环店" ⊂ "小韩面-中环店"）
                    if any(cand in e for e in entities):
                        continue
                    # 通用数量词/虚词放行
                    if any(k in cand for k in ("所有", "全部", "其他", "其他店铺", "同样的",
                                               "同一天", "这个", "那个", "上一轮", "昨天",
                                               "今天", "明天", "最近", "指定", "对应")):
                        continue
                    self.warn(f"{uid}: 输入实体「{cand}」不在实体清单内（若为故意异常可忽略）")

    def run(self, products_path=None):
        names, tools, tool_of = load_ability(self.ability_path)
        self.valid_dims = load_dims(self.req_type)
        self.check_top()
        self.check_ids()
        self.check_fields(names, tools, tool_of)
        self.check_config_consistency(names, tools, tool_of)
        self.check_block_semantic()
        self.check_intent_param_consistency()
        self.check_sample_extra()
        self.check_dup()
        self.check_coverage(names)
        self.check_entities(products_path, names)
        return self.report()

    def report(self):
        print("=" * 72)
        print(f"自检报告: {self.dataset.get('系统')}  [需求类型 {self.req_type}]")
        print(f"用例总数: {len(self.cases)}")
        print("=" * 72)
        for level, icon, label in ((self.f, "F", "致命"), (self.e, "E", "错误"),
                                   (self.w, "W", "警告"), (self.i, "I", "信息")):
            if level:
                print(f"\n[{icon}] {label}（{len(level)}）")
                for m in level:
                    print(f"  {icon}  {m}")
        n_f = len(self.f) + len(self.e)
        print("\n" + "-" * 72)
        if n_f:
            print(f"结论: FAIL（致命 {len(self.f)} / 错误 {len(self.e)}，需修复后再执行）")
        else:
            print(f"结论: PASS（致命 0 / 错误 0；警告 {len(self.w)} 条，请人工确认）")
        return 1 if n_f else 0


def _autodiscover(filter_key=None):
    """--auto 模式：自动发现 ai-test-framework 下的数据集/能力目录/实体清单。
    filter_key 非空时，仅检查 系统名/文件名 包含该关键词的数据集。
    返回 [(dataset, ability, products), ...]。
    """
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # ai-test-framework
    ds_dir = os.path.join(base, "datasets")
    ab_dir = os.path.join(base, "ability")
    dim_dir = os.path.join(base, "dimensions")
    # 加载所有能力目录，按「系统」字段建索引（含文件名校名兜底）
    abilities = {}
    for f in glob.glob(os.path.join(ab_dir, "*.yaml")):
        try:
            doc = load_yaml(f)
            if doc and doc.get("系统"):
                abilities.setdefault(doc["系统"], f)
        except Exception:
            pass
    for f in glob.glob(os.path.join(ab_dir, "*.yaml")):
        base_n = os.path.splitext(os.path.basename(f))[0]
        abilities.setdefault(base_n, f)  # 文件名校名兜底

    products = None
    for f in glob.glob(os.path.join(ab_dir, "*实体清单*.yaml")):
        products = f
        break

    pairs = []
    for f in glob.glob(os.path.join(ds_dir, "*.yaml")):
        try:
            doc = load_yaml(f)
        except Exception:
            continue
        sys_n = (doc or {}).get("系统") or ""
        base_n = os.path.splitext(os.path.basename(f))[0]
        if filter_key and filter_key not in sys_n and filter_key not in base_n:
            continue
        ability = abilities.get(sys_n)
        if ability is None:
            print(f"[F] 数据集 {os.path.basename(f)} 找不到匹配的能力目录（系统={sys_n}）")
            continue
        pairs.append((f, ability, products))
    return pairs, dim_dir


def main():
    global DIM_A, DIM_B, DIM_C, DIM_D, DIM_E
    ap = argparse.ArgumentParser(description="AI 测试数据集自检")
    ap.add_argument("--dataset", default=None, help="数据集 yaml 路径")
    ap.add_argument("--ability", default=None, help="能力目录 yaml 路径")
    ap.add_argument("--products", default=None, help="实体清单 yaml 路径（可选）")
    ap.add_argument("--dims", default=None, help="维度目录路径（默认 scripts/../dimensions）")
    ap.add_argument("--auto", action="store_true", help="自动发现全部数据集并逐一校验")
    ap.add_argument("--filter", default=None, help="仅校验系统名/文件名包含该关键词的数据集（配合 --auto）")
    args = ap.parse_args()

    if args.auto:
        pairs, dim_dir = _autodiscover(args.filter)
        if not pairs:
            print("未发现任何数据集")
            return 1
        rc = 0
        for ds, ab, prod in pairs:
            DIM_A = os.path.join(dim_dir, "A_MCP工具.yaml")
            DIM_B = os.path.join(dim_dir, "B_Agent系统.yaml")
            DIM_C = os.path.join(dim_dir, "C_AgentMCP集成.yaml")
            DIM_D = os.path.join(dim_dir, "D_Skill原子能力.yaml")
            DIM_E = os.path.join(dim_dir, "E_RAG知识库.yaml")
            rc |= Checker(ds, ab).run(prod)
            print()
        return rc

    if not args.dataset or not args.ability:
        ap.error("请提供 --dataset 与 --ability，或使用 --auto")

    dim_dir = args.dims or "dimensions"
    DIM_A = f"{dim_dir}/A_MCP工具.yaml"
    DIM_B = f"{dim_dir}/B_Agent系统.yaml"
    DIM_C = f"{dim_dir}/C_AgentMCP集成.yaml"
    DIM_D = f"{dim_dir}/D_Skill原子能力.yaml"
    DIM_E = f"{dim_dir}/E_RAG知识库.yaml"

    ck = Checker(args.dataset, args.ability)
    return ck.run(args.products)


if __name__ == "__main__":
    import glob
    import os
    sys.exit(main())
