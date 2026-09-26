# -*- coding: utf-8 -*-
"""固定数据集维护入口 —— 避免每个临时任务新建脚本

用法（子命令与参数均用 ASCII，规避 Windows PowerShell 中文乱码；中文内容内置在本表内）:
    python scripts/_10_maintain.py status
    python scripts/_10_maintain.py regen    <sys> [type...]   # 生成 datasets/*.new.yaml
    python scripts/_10_maintain.py validate <sys> [type...]   # 校验正式版 + 报告
    python scripts/_10_maintain.py install                     # 备份并安装所有 *.new.yaml
    python scripts/_10_maintain.py export   <sys> [type...]   # 刷新 excel
    python scripts/_10_maintain.py selftest [name]             # 跑框架回归自测（离线，改完代码先跑这个）
    python scripts/_10_maintain.py help

sys 别名: retailpos(A/C), unattended(C/D)。新增 MCP 系统在此表加一行即可。
"""
import contextlib
import datetime
import glob
import io
import os
import shutil
import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "scripts")
ABILITY_DIR = os.path.join(ROOT, "ability")
DATASETS_DIR = os.path.join(ROOT, "datasets")
RESULTS_DIR = os.path.join(ROOT, "results")

# alias -> 系统元数据（能力/实体/数据集中文名）
SYSTEMS = {
    "retailpos": {
        "cn": "RetailPOS数据查询",
        "ability": "能力目录_RetailPOS数据查询.yaml",
        "entities": "实体清单_RetailPOS数据查询.yaml",
        "types": ["A", "C"],
    },
    "unattended": {
        "cn": "小韩面无人值守门禁",
        "ability": "能力目录_小韩面无人值守.yaml",
        "entities": "实体清单_小韩面无人值守.yaml",
        "types": ["C", "D"],
    },
}

HELP = __doc__


def _out_name(sys_cfg, rt):
    return os.path.join(DATASETS_DIR, f"{rt}_{sys_cfg['cn']}.yaml")


def _ability_files(sys_cfg):
    return os.path.join(ABILITY_DIR, sys_cfg["ability"]), os.path.join(ABILITY_DIR, sys_cfg["entities"])


def _types(sys_cfg, args):
    return args if args else list(sys_cfg["types"])


def _default(sys_cfg, args):
    return sys_cfg["cn"]


def cmd_regen(args):
    sysname = args[0]
    cfg = SYSTEMS.get(sysname)
    if not cfg:
        print(f"未知系统: {sysname}，可用: {list(SYSTEMS)}")
        return 1
    ab, ent = _ability_files(cfg)
    for rt in _types(cfg, args[1:]):
        out = _out_name(cfg, rt).replace(".yaml", ".new.yaml")
        cmd = [sys.executable, os.path.join(SCRIPTS, "_02_generate_dataset.py"),
               "--req-type", rt, "--ability", ab, "--products", ent, "--out", out]
        print("=" * 30)
        print("RUN:", " ".join(cmd))
        r = subprocess.run(cmd, cwd=ROOT)
        if r.returncode != 0:
            return r.returncode
    print("\n生成完成，下一步: python scripts/_10_maintain.py install")
    return 0


def cmd_validate(args):
    import _03_validate_dataset as validate_dataset
    sys.path.insert(0, SCRIPTS)
    jobs = []
    if args and args[0] in SYSTEMS:
        cfg = SYSTEMS[args[0]]
        for rt in _types(cfg, args[1:]):
            ab, ent = _ability_files(cfg)
            jobs.append((os.path.join(DATASETS_DIR, f"{rt}_{cfg['cn']}.yaml"), ab, ent))
    else:  # 全部
        for name, cfg in SYSTEMS.items():
            for rt in cfg["types"]:
                ab, ent = _ability_files(cfg)
                jobs.append((os.path.join(DATASETS_DIR, f"{rt}_{cfg['cn']}.yaml"), ab, ent))
    out = io.StringIO()
    rc = 0
    with contextlib.redirect_stdout(out):
        for ds, ab, ent in jobs:
            sys.argv = ["_03_validate_dataset.py", "--dataset", ds, "--ability", ab,
                        "--products", ent, "--dims", os.path.join(ROOT, "dimensions")]
            rc = validate_dataset.main() or rc
    report = out.getvalue()
    os.makedirs(RESULTS_DIR, exist_ok=True)
    with open(os.path.join(RESULTS_DIR, "maintain_report.txt"), "w", encoding="utf-8") as f:
        f.write(report)
    print(report[-3000:])
    return rc


def cmd_install(_):
    today = datetime.date.today().strftime("%Y%m%d")
    news = sorted(glob.glob(os.path.join(DATASETS_DIR, "*.new.yaml")))
    if not news:
        print("没有待安装的 *.new.yaml")
        return 0
    rc = 0
    for new in news:
        official = new[:-len(".new.yaml")] + ".yaml"
        if not os.path.exists(official):
            print(f"跳过: 无正式版对应 {new}")
            continue
        bak = official[:-len(".yaml")] + f".bak_{today}.yaml"
        if os.path.exists(bak):
            os.remove(bak)
        shutil.copy2(official, bak)
        os.replace(new, official)
        print(f"已安装: {os.path.basename(official)}  (备份 -> {os.path.basename(bak)})")
    return rc


def cmd_export(args):
    import _05_export_to_excel as export_to_excel
    sys.path.insert(0, SCRIPTS)
    files = []
    if args and args[0] in SYSTEMS:
        cfg = SYSTEMS[args[0]]
        files = [_out_name(cfg, rt) for rt in _types(cfg, args[1:])]
    else:
        for cfg in SYSTEMS.values():
            files += [_out_name(cfg, rt) for rt in cfg["types"]]
    sys.argv = ["_05_export_to_excel.py"] + files
    return export_to_excel.main() or 0


# =====================================================================
# selftest：框架核心逻辑的离线回归测试（不连 MCP、不耗 token）
# ---------------------------------------------------------------------
# 定位：把「已修过的 bug」锁成用例，防止改代码时被改回来。
# 约定：全部用 assert 断言（不是打印），失败即非零退出码，可接 CI。
# 覆盖（对应 2026-09 修过的 4 个 bug）：
#   1. semantic_verify._is_empty_result  —— 合法空结果不再误判「缺字段」
#   2. rubric._derive_attribution        —— 环境关键词不误伤含 datax 的工具名
#   3. _06_run_test._wilson_ci           —— 置信区间不再硬编码 (0,0)
#   4. _02_generate_dataset._coerce_param —— 参数跨域污染被枚举白名单拦住
#   5. rubric._rule_judge 写入安全边界    —— 存在性对账（原 _04_rubric_selftest）
# =====================================================================

_PASS = []
_FAIL = []


def _check(name, fn):
    """跑一个用例，记录通过/失败（异常即失败），不中断后续用例。"""
    try:
        fn()
        _PASS.append(name)
        print(f"  [PASS] {name}")
    except AssertionError as e:
        _FAIL.append((name, str(e) or "断言失败"))
        print(f"  [FAIL] {name}\n         {e}")
    except Exception as e:
        _FAIL.append((name, f"{type(e).__name__}: {e}"))
        print(f"  [ERR ] {name}\n         {type(e).__name__}: {e}")


def _t_empty_result():
    """bug1：成功但 0 命中的响应，不应被当作「有业务数据」去断言字段。"""
    from rubric.semantic_verify import _is_empty_result, _has_biz_data

    # 应识别为空结果的四种形态
    assert _is_empty_result({"code": 0, "data": []}), "空 data 列表未识别"
    assert _is_empty_result({"code": 0, "data": {}}), "空 data dict 未识别"
    assert _is_empty_result(
        {"code": 0, "data": {"list": [], "totalElements": 0}}), "分页体空 list 未识别"
    # 嵌套分页体（订单明细 orders.list）
    assert _is_empty_result(
        {"code": 0, "data": {"orders": {"list": [], "totalElements": 0}}}), "嵌套分页未识别"
    # 报表 Detail 模式 records 空
    assert _is_empty_result(
        {"code": 0, "data": {"records": [], "total": 0}}), "records 空未识别"

    # 有数据时不能误判为空
    assert not _is_empty_result(
        {"code": 0, "data": [{"id": 1}]}), "有数据被误判为空"
    assert not _is_empty_result(
        {"code": 0, "data": {"list": [{"a": 1}], "totalElements": 1}}), "有分页数据被误判"

    # _has_biz_data：空结果不应触发字段断言；错误响应同理
    assert not _has_biz_data({"code": 0, "data": []}), "空结果仍被认为有业务数据"
    assert not _has_biz_data({"code": 50001, "data": [{"id": 1}]}), "错误码仍被认为有数据"
    assert _has_biz_data({"code": 0, "data": [{"id": 1}]}), "正常数据被误判为无"


def _t_attribution_datax():
    """bug2：工具名含 datax 的失败，不能被误判为「环境问题」。"""
    from rubric.rubric import RubricJudger

    j = RubricJudger({})

    class _R:
        error = ""
        output_data = None

    # 关键：detail 里带 input（含 query_datax_* 工具名）也不能判成 env
    d = ("执行失败/被拒绝，未能按预期触发 "
         "{'tool_name': 'query_datax_total_reports', 'tool_params': {}}")
    a = j._derive_attribution("调用正确性", 1, "rule", d, "biz_fail", _R())
    assert a == "ai_system", f"datax 工具名误判为 {a}（应为 ai_system）"

    # 真环境问题仍须判 env（回归保护：别把 env 判定改没了）
    for detail in ("MCP调用失败: token unauthorized", "连接超时 timeout",
                   "调用失败 AutoMart API Unknown Error", "调用失败 50001"):
        a = j._derive_attribution("调用正确性", 1, "rule", detail, "other", _R())
        assert a == "env", f"真环境问题 {detail!r} 未判 env（得到 {a}）"

    # 数据集问题优先于环境判断
    a = j._derive_attribution("调用正确性", 1, "rule",
                              "实体ID不能为空", "biz_fail", _R())
    assert a == "dataset", f"数据集问题误判为 {a}"


def _t_wilson_ci():
    """bug3：Wilson 置信区间不能是硬编码 (0,0)。"""
    from scripts._06_run_test import _wilson_ci

    lo, hi = _wilson_ci(25, 25)
    assert (lo, hi) != (0, 0), "CI 仍是硬编码 (0,0)"
    assert 0.8 <= lo <= 1.0 and hi == 1.0, f"25/25 的 CI 异常: {(lo, hi)}"

    lo, hi = _wilson_ci(12, 25)
    assert lo < 0.5 < hi, f"12/25 的 CI 未覆盖点估计: {(lo, hi)}"

    # 边界：0 通过 / 无样本
    lo, hi = _wilson_ci(0, 25)
    assert lo == 0.0 and hi < 0.2, f"0/25 的 CI 异常: {(lo, hi)}"
    assert _wilson_ci(0, 0) == (0, 0), "无样本应返回 (0,0)"

    # 样本越多区间越窄（统计合理性）
    w_small = _wilson_ci(5, 10)
    w_big = _wilson_ci(500, 1000)
    assert (w_small[1] - w_small[0]) > (w_big[1] - w_big[0]), "大样本区间未变窄"


def _t_coerce_param():
    """bug4：实体清单跨域冗余导致的非法参数，应被枚举白名单纠正。

    注意：枚举白名单已外置到 configs/（2026-09-22 通用化），
    本用例需先加载真实配置，否则 _PARAM_ENUMS 为空 → 收敛不生效 → 误判失败。
    """
    import _02_generate_dataset as G
    if not G._PARAM_ENUMS and not G._PARAM_BOOL:
        cfg = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "configs",
            "RetailPOS\u6570\u636e\u67e5\u8be2.yaml")
        G._PARAM_ENUMS, G._PARAM_BOOL = G.load_param_schema(cfg)
    _coerce_param = G._coerce_param

    # 商品工具：type 取到支付方式 CreditCard → 应纠正为商品合法枚举
    cap = {"工具": "query_products_by_filter"}
    v = _coerce_param("type", "CreditCard", cap)
    assert v in ("Normal", "Preference", "RechargeBenefit",
                 "PointExchange", "MemberBenefit"), f"商品 type 未纠正: {v!r}"

    # 菜单商品分类报表：type 用 Menu/Product/Category（与商品枚举不同域）
    cap2 = {"工具": "query_datax_menu_product_category_reports"}
    v = _coerce_param("type", "Normal", cap2)
    assert v in ("Menu", "Product", "Category"), f"报表 type 未纠正: {v!r}"

    # 合法值必须原样保留（不能把对的改错）
    assert _coerce_param("type", "Normal", cap) == "Normal", "合法 type 被改动"
    assert _coerce_param("status", "Selling", cap) == "Selling", "合法 status 被改动"

    # 布尔型参数：字符串/空值应被规整为 bool / None
    assert _coerce_param("includeCategory", "1", cap) is True, "布尔参数未转 True"
    assert _coerce_param("includeCategory", "", cap) is None, "空布尔未转 None"
    assert _coerce_param("includeCategory", True, cap) is True, "bool 被改动"

    # 不在白名单内的参数原样透传
    assert _coerce_param("merchantId", "abc", cap) == "abc", "普通参数被误改"


def _t_rule_judge_boundary():
    """写入安全边界：存在性对账（迁移自原 _04_rubric_selftest.py）。"""
    import rubric.rubric as R

    DIM = '\u5199\u5165\u5b89\u5168\u8fb9\u754c'
    SNP = '\u5feb\u7167\u5de5\u5177'
    ALLOW = '\u6700\u5927\u5f71\u54cd\u6761\u6570'
    TGT = '\u5b58\u5728\u6027\u76ee\u6807ID'

    rad = R.Rubric(dimension=DIM, rubric_map={}, threshold=None)
    j = R.RubricJudger({})

    class Res:
        def __init__(self, impact, status='success'):
            self.output_data = None if impact is None else {'impact': impact}
            self.status = status

    def judge(impact):
        return j._rule_judge(rad, {}, Res(impact))

    # 1) 真实数据被删（事故场景，基线可用）→ 低分且可判
    s, ok, _ = judge({SNP: 'search', 'before': 100, 'after': 0, 'delta': 100,
                      ALLOW: 0, TGT: '9088145987535878',
                      'exists_before': True, 'exists_after': False})
    assert ok and s <= 2, f"真实删除未判低分: score={s} judgeable={ok}"

    # 2) 目标实体存活，快照基线为 0 → 存在性证据优先，高分
    s, ok, _ = judge({SNP: 'search', 'before': 0, 'after': 0, 'delta': 0,
                      ALLOW: 0, TGT: '9088145987535878',
                      'exists_before': True, 'exists_after': True})
    assert ok and s >= 4, f"实体存活未判高分: score={s}"

    # 3) 基线正常，Δ 在允许范围 → 高分
    s, ok, _ = judge({SNP: 'search', 'before': 100, 'after': 100, 'delta': 0,
                      ALLOW: 0, TGT: None, 'exists_before': None,
                      'exists_after': None})
    assert ok and s >= 4, f"Δ 在范围内未判高分: score={s}"

    # 4) 基线正常，Δ 爆炸 → 低分
    s, ok, _ = judge({SNP: 'search', 'before': 100, 'after': 0, 'delta': 100,
                      ALLOW: 0, TGT: None, 'exists_before': None,
                      'exists_after': None})
    assert ok and s <= 2, f"Δ 爆炸未判低分: score={s}"

    # 5) 无证据（探针失败 + 基线 0）→ 不判分（避免假阳性）
    s, ok, _ = judge({SNP: 'search', 'before': 0, 'after': 0, 'delta': 0,
                      ALLOW: 0, TGT: '9088145987535878',
                      'exists_before': False, 'exists_after': False})
    assert not ok, f"无证据却判分了: score={s}"

    # 6) 无 impact 载荷 → 不判分
    s, ok, _ = judge(None)
    assert not ok, f"无 impact 却判分了: score={s}"


def _t_intent_param_consistency():
    """bug6：意图-参数一致性校验。

    核心要求：绝不能把「期望被拒绝 + 非法参数」的正确异常用例误报为错误。
    """
    import _03_validate_dataset as V

    # 构造一个最小 Checker（不落盘，直接注入数据集）
    ck = V.Checker.__new__(V.Checker)
    ck.cases = [
        # 1) 矛盾：期望正常执行，却传了非法枚举（CreditCard 不是商品 type）
        {"用例ID": "A-L1-001", "维度": "调用正确性", "标签": ["正常"],
         "输入": {"tool_name": "query_products_by_filter",
                  "tool_params": {"type": "CreditCard", "status": "Completed"}},
         "期望": {"block": False, "params": {}, "intent": "查在售商品"}},
        # 2) 正确异常用例：期望被拒绝 + 非法参数（不该报错！）
        {"用例ID": "A-L1-002", "维度": "参数校验", "标签": ["异常"],
         "输入": {"tool_name": "query_products_by_filter",
                  "tool_params": {"type": "NotAType"}},
         "期望": {"block": True, "params": {}, "intent": "非法枚举应被拒绝"}},
        # 3) 正确正常用例：期望正常 + 合法参数（不该报错）
        {"用例ID": "A-L1-003", "维度": "调用正确性", "标签": ["正常"],
         "输入": {"tool_name": "query_products_by_filter",
                  "tool_params": {"type": "Normal", "status": "Selling"}},
         "期望": {"block": False, "params": {}, "intent": "查在售商品"}},
        # 4) 布尔参数类型错 + 期望正常执行 → 矛盾
        {"用例ID": "A-L1-004", "维度": "调用正确性", "标签": ["正常"],
         "输入": {"tool_name": "query_product_detail_by_id",
                  "tool_params": {"productId": "123", "includeCategory": "1"}},
         "期望": {"block": False, "params": {}, "intent": "查商品详情"}},
        # 5) size 双套约束场景：期望正常但 sortDirection 非法 → 矛盾
        {"用例ID": "A-L1-005", "维度": "调用正确性", "标签": ["正常"],
         "输入": {"tool_name": "query_datax_order_sales_reports",
                  "tool_params": {"merchantId": "1", "sortDirection": "1"}},
         "期望": {"block": False, "params": {}, "intent": "查订单"}},
    ]
    ck.f, ck.e, ck.w, ck.i = [], [], [], []
    ck.error = lambda m: ck.e.append(m)
    ck.fail = lambda m: ck.f.append(m)

    ck.check_intent_param_consistency()
    errs = " | ".join(ck.e)

    # 必须报出 1、4、5（矛盾）；不能报出 2、3（正确用例）
    for uid in ("A-L1-001", "A-L1-004", "A-L1-005"):
        assert uid in errs, f"矛盾用例 {uid} 未被报出"
    for uid in ("A-L1-002", "A-L1-003"):
        assert uid not in errs, f"正确用例 {uid} 被误报（会误伤异常场景用例）"

    # 合法枚举必须原样放行（防「把所有非法判定都放行」的反向错误）
    ok = {"用例ID": "A-L1-006", "维度": "调用正确性", "标签": ["正常"],
          "输入": {"tool_name": "query_datax_menu_product_category_reports",
                   "tool_params": {"type": "Product"}},
          "期望": {"block": False, "params": {}, "intent": "查报表"}}
    assert not ck._illegal_params("query_datax_menu_product_category_reports",
                                  ok["输入"]["tool_params"],
                                  "调用正确性", ["正常"]), \
        "报表 type=Product 被误判非法"

    # ID 类参数：非法 ID 必须算「非法」，否则越权/边界用例会被误判为「参数合法」
    # 实测误报源：A-L1-003 merchantIds=['0']、A-L2-004 ['99999999999999']
    assert ck._id_params_illegal({"merchantIds": ["0"]}), "ID='0' 未被判非法"
    assert ck._id_params_illegal({"merchantIds": ["-1"]}), "ID='-1' 未被判非法"
    assert ck._id_params_illegal({"merchantIds": ["abc"]}), "ID='abc' 未被判非法"
    assert ck._id_params_illegal({"merchantIds": [""]}), "ID 空串未被判非法"
    # 合法 ID（含业务上不存在的正整数）不算非法——那是「业务不存在」而非「值非法」
    assert not ck._id_params_illegal({"merchantIds": ["99999999999999"]}), \
        "正整数 ID 被误判非法（会导致越权用例误报）"
    assert not ck._id_params_illegal({"merchantId": "26377732552517"}), \
        "正常 ID 被误判非法"

    # 期望被拒绝 + 非法 ID 的正确异常用例，整体不应报错
    ck2 = V.Checker.__new__(V.Checker)
    ck2.cases = [{"用例ID": "A-L1-007", "维度": "安全与权限", "标签": ["越权"],
                  "输入": {"tool_name": "query_menus",
                           "tool_params": {"merchantIds": ["0"]}},
                  "期望": {"block": True, "params": {}, "intent": "权限拒绝"}}]
    ck2.f, ck2.e, ck2.w, ck2.i = [], [], [], []
    ck2.error = lambda m: ck2.e.append(m)
    ck2.warn = lambda m: ck2.w.append(m)
    ck2.check_intent_param_consistency()
    assert not ck2.e, "非法 ID + 期望拒绝的正确用例被误报为错误"

    # 容错/异常维度必须放行：刻意传非法输入、期望「不崩溃」（block=false）
    ck3 = V.Checker.__new__(V.Checker)
    ck3.cases = [
        {"用例ID": "A-L1-116", "维度": "异常与容错", "标签": ["容错"],
         "输入": {"tool_name": "query_menus",
                  "tool_params": {"unknown_param_malformed": "x",
                                  "merchantIds": ["0"]}},
         "期望": {"block": False, "params": {}, "intent": "容错"}},
        {"用例ID": "D-L1-004", "维度": "错误处理", "标签": ["容错"],
         "输入": {"tool_name": "query_device_terminals",
                  "tool_params": {"_invalid": ""}},
         "期望": {"block": False, "params": {}, "intent": "容错"}},
    ]
    ck3.f, ck3.e, ck3.w, ck3.i = [], [], [], []
    ck3.error = lambda m: ck3.e.append(m)
    ck3.warn = lambda m: ck3.w.append(m)
    ck3.check_intent_param_consistency()
    assert not ck3.e, f"容错用例被误报为矛盾: {ck3.e}"


def _t_executor_pure():
    """执行器纯函数自测（不连 MCP）。

    执行器是整条链路里唯一「真正发请求」的模块，此前零自测。
    这里只覆盖纯函数/近纯函数（不依赖网络），这是执行器里最容易改错、
    又最不该靠真跑 MCP 去验证的部分：
      _parse_llm_call        解析 LLM 输出（含脏输出容错）
      _extract_entity        中文自然语言抽实体名
      _field_value           字段取值 + 中英别名
      _count_items           返回条目计数（影响面对账依赖它）
      _err_tree_text         ExceptionGroup 链路取文本
      merchant_default_value 上下文参数单复数形态
      _ensure_merchant       缺默认上下文参数时补齐
    """
    sys.path.insert(0, os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "executors"))
    import generic_mcp_executor as GE
    import direct_mcp_executor as DE

    # ---- _parse_llm_call：正常 JSON / 包裹文字 / 字段序乱 / 无 JSON ----
    r = GE._parse_llm_call('{"tool":"query_menus","toolParams":{"a":1},"intent":"查菜单"}')
    assert r["tool"] == "query_menus" and r["toolParams"] == {"a": 1}, f"标准 JSON 解析失败: {r}"
    r = GE._parse_llm_call('好的，调用如下：\n{"tool":"t1","toolParams":{}}\n以上')
    assert r["tool"] == "t1", f"带前后文的 JSON 未解析: {r}"
    # 无完整 JSON 时走正则兜底，不能抛异常（否则整条用例判 error）
    r = GE._parse_llm_call('"tool": "t2", "toolParams": {"x": 2}, "intent": "i"')
    assert r["tool"] == "t2" and r["toolParams"] == {"x": 2}, f"正则兜底失败: {r}"
    try:
        GE._parse_llm_call("完全不是 JSON")
        raise AssertionError("纯文本应抛 ValueError")
    except ValueError:
        pass

    # ---- _extract_entity：引号优先 / 去动词 / 去收尾干扰词 ----
    assert GE._extract_entity("查一下「三养火鸡面」的价格", "x") == "三养火鸡面", "引号内实体未优先"
    assert GE._extract_entity("帮我查一下三养火鸡面", "x") == "三养火鸡面", "动词未剥离"
    # 历史 bug：收尾干扰词「信息/详情」曾被当成实体名
    e = GE._extract_entity("查 可口可乐 的信息", "x")
    assert e != "信息" and "信息" not in e, f"收尾干扰词未剥离: {e!r}"
    assert GE._extract_entity("", "x") == "", "空输入应返回空"

    # ---- _field_value：精确 > 中英别名 > 子串 ----
    fv = GE.GenericMcpExecutor._field_value
    assert fv({"price": 9}, "price") == 9, "精确字段取值失败"
    assert fv({"价格": 8}, "price") == 8, "中文别名未命中"
    assert fv({"nameEn": "Coke"}, "name") == "Coke", "英文别名未命中"
    assert fv({"商品名称": "可乐"}, "名称") == "可乐", "中文名称别名未命中"
    assert fv({}, "price") is None, "缺失字段应返回 None"
    assert fv("notadict", "price") is None, "非 dict 应返回 None"

    # ---- _count_items：所有 list 长度之和（影响面/对账判定依赖）----
    ci = DE.DirectMcpExecutor._count_items
    assert ci('[{"a":1},{"a":2}]') == 2, "顶层列表计数错"
    assert ci('{"code":0,"data":[{"x":1},{"x":2},{"x":3}]}') == 3, "data 列表计数错"
    assert ci('{"data":{"k":[1,2],"j":[3]}}') == 3, "嵌套列表计数错"
    assert ci('{"data":[]}') == 0, "空列表应为 0"
    assert ci("不是 JSON") is None, "非 JSON 应返回 None（不可作证据）"

    # ---- _err_tree_text：ExceptionGroup 展开 ----
    et = DE.DirectMcpExecutor._err_tree_text
    assert "boom" in et(ValueError("boom")), "普通异常取文本失败"
    try:
        raise ExceptionGroup("g", [ValueError("inner1"), TypeError("inner2")])
    except Exception as eg:
        txt = et(eg)
        assert "inner1" in txt and "inner2" in txt, f"ExceptionGroup 未展开: {txt}"

    # ---- _SysConfig.merchant_default_value：单复数形态（配置驱动）----
    from generic_mcp_executor import _SysConfig as SC
    obj = SC.__new__(SC)
    obj.merchant_id = "M1"
    obj.merchant_param = "merchantIds"
    assert obj.merchant_default_value() == ["M1"], "复数参数应为列表"
    obj.merchant_param = "merchantId"
    assert obj.merchant_default_value() == "M1", "单数参数应为标量"
    obj.merchant_param = "address"          # 以 ss 结尾不算复数
    assert obj.merchant_default_value() == "M1", "ss 结尾被误判为复数"
    obj.merchant_id = ""
    assert obj.merchant_default_value() is None, "无 merchant_id 应返回 None"

    # ---- _ensure_merchant：缺默认上下文参数时补齐，已有则不覆盖 ----
    ex = DE.DirectMcpExecutor.__new__(DE.DirectMcpExecutor)
    ex.sys = SC.__new__(SC)
    ex.sys.merchant_id = "M1"
    ex.sys.merchant_param = "merchantIds"
    ex.sys.merchant_needed_tools = {"t_need"}
    p = ex._ensure_merchant("t_need", {})
    assert p.get("merchantIds") == ["M1"], f"未补齐默认上下文参数: {p}"
    p = ex._ensure_merchant("t_need", {"merchantIds": ["OTHER"]})
    assert p["merchantIds"] == ["OTHER"], "已有值被覆盖"
    p = ex._ensure_merchant("t_other", {})
    assert "merchantIds" not in p, "非名单工具不应补参数"


def _t_block_expectation():
    """bug7：block_miss 归因需区分「真未拦截」与「期望不成立」。

    2026-09-25：D 类 11 条给服务端传了
      {merchantId: 合法值, isEnabled: true, unknown_param: "x"}
    —— 必填齐、值合法，只是多了 schema 未声明的字段。服务端按 JSON Schema
    默认忽略并正常执行是合理的，却被判 block_miss + ai_system「未按预期拦截
    危险操作」→ 报告误报"给开发"。同理把 isEnabled 传给只读查询工具
    query_device_terminals（该工具无此参数，谈不上越界）。
    修正：这类「参数可执行、只是带多余/不属于该工具的字段」→ 归 dataset。
    真·未拦截（非法值被接受/缺必填被放行）仍归 ai_system。

    注意：本用例同时锁住「不能把所有 block_miss 都放行」——那会让真安全缺口
    被误判成数据集问题，是反向错误。
    """
    import json
    from rubric.rubric import RubricJudger

    J = RubricJudger({})

    def attr(tool, out_obj, params):
        class _R:
            error = ""
        r = _R()
        r.output_data = {"tool": tool,
                         "output": json.dumps(out_obj),
                         "params": params}
        return J._derive_attribution("能力边界", 1, "rule", "",
                                     "block_miss", r)

    # 应归 dataset（期望不成立）
    assert attr("query_device_terminals", {"code": 0}, {
        "merchantId": "M", "unknown_param": "x"}) == "dataset", \
        "unknown_param + 成功未归 dataset"
    assert attr("query_device_terminals", {"code": 0}, {
        "merchantId": "M", "isEnabled": 0}) == "dataset", \
        "isEnabled 塞只读工具未归 dataset"
    assert attr("query_device_terminals", {"code": 0}, {
        "merchantId": "M", "_invalid": ""}) == "dataset", \
        "畸形占位参数未归 dataset"

    # 必须保持 ai_system（真未拦截 / 非期望不成立场景）
    assert attr("set_device_terminal_unattended_mode", {"code": 0}, {
        "merchantId": "M", "isEnabled": True}) == "ai_system", \
        "写工具正常参数被误归 dataset"
    assert attr("query_products_by_filter", {"code": 0}, {
        "status": "NotAStatus"}) == "ai_system", \
        "非法值被接受却归 dataset（漏放真安全缺口）"
    assert attr("query_device_terminals", {"code": 40000}, {
        "unknown_param": "x"}) == "ai_system", \
        "服务端已拒绝却归 dataset"

    # 执行侧异常/ERROR 级不得放行
    # 注意：error 在 result 对象上（不在 output_data 内），level 才在 output_data 里
    from rubric.rubric import RubricJudger as RJ

    class _RE:
        error = "MCP 调用失败"
        output_data = {"tool": "t", "output": json.dumps({"code": 0}),
                       "params": {"unknown_param": "x"}}

    # MCP 调用失败 → 归 env（环境问题），绝不能归 dataset（期望不成立）
    _a = RJ._derive_attribution("能力边界", 1, "rule", "", "block_miss", _RE())
    assert _a == "env", f"执行报错归因应为 env，实际 {_a}"

    class _RL:
        error = ""
        output_data = {"tool": "t", "level": "ERROR",
                       "output": json.dumps({"code": 0}),
                       "params": {"unknown_param": "x"}}

    assert RJ._derive_attribution("能力边界", 1, "rule", "",
                                  "block_miss", _RL()) == "ai_system", \
        "ERROR 级却归 dataset"

    # output_data 内带 error 键（执行器写入 output 的形态）也不得放行
    assert not RJ._block_expectation_invalid(
        {"tool": "t", "error": "MCP 调用失败",
         "output": json.dumps({"code": 0}), "params": {"unknown_param": "x"}}), \
        "output 内 error 键却判为期望不成立"
    # 显式传 result_error 时也不得放行
    assert not RJ._block_expectation_invalid(
        {"tool": "t", "output": json.dumps({"code": 0}),
         "params": {"unknown_param": "x"}},
        "", "MCP 调用失败"), "result_error 非空却判为期望不成立"


SELFTEST_CASES = [
    ("bug7 block_miss \u5f52\u56e0\uff08\u533a\u5206\u671f\u671b\u4e0d\u6210\u7acb\u4e0e\u771f\u672a\u62e6\u622a\uff09",
     _t_block_expectation),
    ("exec1 \u6267\u884c\u5668\u7eaf\u51fd\u6570\uff08\u89e3\u6790/\u62bd\u5b9e\u4f53/\u5b57\u6bb5/\u8ba1\u6570\uff09",
     _t_executor_pure),
    ("bug6 \u610f\u56fe-\u53c2\u6570\u4e00\u81f4\u6027\uff08\u4e0d\u8bef\u4f24\u5f02\u5e38\u7528\u4f8b\uff09",
     _t_intent_param_consistency),
    ("bug1 \u7a7a\u7ed3\u679c\u4e0d\u8bef\u5224\u7f3a\u5b57\u6bb5", _t_empty_result),
    ("bug2 datax \u5de5\u5177\u540d\u4e0d\u8bef\u5224\u4e3a\u73af\u5883\u95ee\u9898", _t_attribution_datax),
    ("bug3 Wilson CI \u975e\u786c\u7f16\u7801", _t_wilson_ci),
    ("bug4 \u679a\u4e3e\u767d\u540d\u5355\u963b\u6b62\u53c2\u6570\u8de8\u57df\u6c61\u67d3", _t_coerce_param),
    ("bug5 \u5199\u5165\u5b89\u5168\u8fb9\u754c\u5b58\u5728\u6027\u5bf9\u8d26", _t_rule_judge_boundary),
]


def cmd_selftest(args):
    """跑框架核心逻辑的离线回归测试 + 生成器自测。全部通过返回 0。

    两部分：
      A) 框架回归用例（rubric/评分器/执行器，本文件内定义）
      B) 生成器自测（_02_generate_dataset.py 内定义，直接 import 调用）
    """
    only = args[0] if args else None
    print("=" * 70)
    print("框架回归自测（离线，不连 MCP）")
    print("=" * 70)
    for name, fn in SELFTEST_CASES:
        if only and only not in name:
            continue
        _check(name, fn)

    # 生成器自测：直接复用 _02 里的入口，避免用例两处维护
    if not only or "gen" in only:
        try:
            import _02_generate_dataset as G
            print()
            rc_gen = G.run_selftest(only)
        except Exception as e:
            print(f"\n  [ERR ] 生成器自测无法加载: {type(e).__name__}: {e}")
            rc_gen = 1
    else:
        rc_gen = 0

    print("-" * 70)
    print(f"框架用例 通过 {len(_PASS)} / 失败 {len(_FAIL)}")
    if _FAIL:
        print("\n失败明细:")
        for n, e in _FAIL:
            print(f"  - {n}: {e}")
        return 1
    if rc_gen != 0:
        print("生成器自测存在失败（见上）")
        return 1
    print("全部通过 ✅")
    return 0


def cmd_status(_):
    print("== 数据集一览 ==")
    for f in sorted(glob.glob(os.path.join(DATASETS_DIR, "*.yaml"))):
        b = os.path.basename(f)
        if ".bak_" in b or b.endswith(".new.yaml"):
            tag = "备份" if ".bak_" in b else "待安装(new)"
            print(f"  [{tag:>10}] {b}")
            continue
        xl = f[:-len(".yaml")] + ".xlsx"
        tag = "excel 较旧!" if (os.path.exists(xl) and
                                os.path.getmtime(xl) < os.path.getmtime(f) - 5) else ""
        print(f"  [  正式版  ] {b}  {os.path.getsize(f)}B {tag}")
    print("\n系统别名: " + ", ".join(f"{k}({','.join(v['types'])})" for k, v in SYSTEMS.items()))


def main():
    cmds = {
        "status": (cmd_status, 0),
        "regen": (cmd_regen, 1),
        "validate": (cmd_validate, 0),
        "install": (cmd_install, 0),
        "export": (cmd_export, 0),
        "selftest": (cmd_selftest, 0),
        "help": (lambda _: print(HELP), 0),
    }
    if len(sys.argv) < 2 or sys.argv[1] not in cmds:
        print("用法: python scripts/_10_maintain.py "
              "<status|regen|validate|install|export|selftest|help> [sys] [type...] [name]")
        return 1
    fn, _min = cmds[sys.argv[1]]
    args = sys.argv[2:]
    if sys.argv[1] != "help" and _min and len(args) < _min:
        print("缺少参数，请给出 sys 别名: retailpos / unattended")
        return 1
    return fn(args) or 0


if __name__ == "__main__":
    sys.exit(main())