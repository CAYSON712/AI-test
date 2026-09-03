# -*- coding: utf-8 -*-
"""固定数据集维护入口 —— 避免每个临时任务新建脚本

用法（子命令与参数均用 ASCII，规避 Windows PowerShell 中文乱码；中文内容内置在本表内）:
    python scripts/_10_maintain.py status
    python scripts/_10_maintain.py regen    <sys> [type...]   # 生成 datasets/*.new.yaml
    python scripts/_10_maintain.py validate <sys> [type...]   # 校验正式版 + 报告
    python scripts/_10_maintain.py install                     # 备份并安装所有 *.new.yaml
    python scripts/_10_maintain.py export   <sys> [type...]   # 刷新 excel
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
        "help": (lambda _: print(HELP), 0),
    }
    if len(sys.argv) < 2 or sys.argv[1] not in cmds:
        print("用法: python scripts/_10_maintain.py <status|regen|validate|install|export|help> [sys] [type...]")
        return 1
    fn, _min = cmds[sys.argv[1]]
    args = sys.argv[2:]
    if sys.argv[1] != "help" and _min and len(args) < _min:
        print("缺少参数，请给出 sys 别名: retailpos / unattended")
        return 1
    return fn(args) or 0


if __name__ == "__main__":
    sys.exit(main())