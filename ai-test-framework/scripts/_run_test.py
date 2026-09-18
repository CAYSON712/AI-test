# -*- coding: utf-8 -*-
"""Run datasets with CJK paths built in Python (shell mangles CJK args on Windows).

Usage:
    python scripts/_run_test.py <job> [--llm-judge]

jobs:
    retail   -> A_RetailPOS数据查询      (A 类, real executor)
    unattend -> D_小韩面无人值守门禁      (D 类, real executor)
    both     -> run both sequentially
"""
import argparse
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
sys.path.insert(0, _HERE)
sys.stdout.reconfigure(encoding="utf-8")

DS = os.path.join(_ROOT, "datasets")
RES = os.path.join(_ROOT, "results")

JOBS = {
    "retail": {
        "req_type": "A",
        "dataset": os.path.join(DS, "A_RetailPOS\u6570\u636e\u67e5\u8be2.yaml"),
        "system": "RetailPOS\u6570\u636e\u67e5\u8be2",
        "out": os.path.join(RES, "_run_retail.json"),
    },
    "unattend": {
        "req_type": "D",
        "dataset": os.path.join(DS, "D_\u5c0f\u97e9\u9762\u65e0\u4eba\u503c\u5b88\u95e8\u7981.yaml"),
        "system": "\u5c0f\u97e9\u9762\u65e0\u4eba\u503c\u5b88\u95e8\u7981",
        "out": os.path.join(RES, "_run_unattend.json"),
    },
}

ap = argparse.ArgumentParser()
ap.add_argument("job", choices=list(JOBS) + ["both"])
ap.add_argument("--runs", type=int, default=1)
ap.add_argument("--executor", default="real")
ap.add_argument("--llm-judge", action="store_true")
ap.add_argument("--report", action="store_true")
a = ap.parse_args()

from scripts._06_run_test import run_dataset  # noqa: E402

names = list(JOBS) if a.job == "both" else [a.job]
for n in names:
    j = JOBS[n]
    print("")
    print("#" * 74)
    print("# job={}  req_type={}".format(n, j["req_type"]))
    print("# dataset : {}  [{}]".format(os.path.basename(j["dataset"]),
                                        os.path.exists(j["dataset"])))
    print("# system  : {}".format(j["system"]))
    print("# executor: {}  runs={}".format(a.executor, a.runs))
    print("#" * 74)
    if not os.path.exists(j["dataset"]):
        print("[SKIP] dataset missing")
        continue
    t0 = time.time()
    run_dataset(j["req_type"], j["dataset"], a.executor, a.runs, j["out"],
                j["system"], report_trace=False, use_llm_judge=a.llm_judge,
                llm_detail=a.llm_judge, auto_report=a.report)
    print("[done] {:.1f}s -> {}".format(time.time() - t0, j["out"]))
