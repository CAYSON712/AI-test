# -*- coding: utf-8 -*-
"""Unit-check the new write-boundary judging logic (existence audit)."""
import glob
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")
base = glob.glob(r'C:\Users\CAYSON.Q\CodeBuddy\*\ai-test-framework')[0]
sys.path.insert(0, base)
import rubric.rubric as R  # noqa: E402

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


def show(tag, impact):
    s, ok, d = j._rule_judge(rad, {}, Res(impact))
    print(f'{tag}\n    score={s} judgeable={ok}\n    {d}')


show('1) REAL DATA DELETED (incident scenario, baseline usable)',
     {SNP: 'search', 'before': 100, 'after': 0, 'delta': 100, ALLOW: 0,
      TGT: '9088145987535878', 'exists_before': True, 'exists_after': False})

show('2) REAL ENTITY SURVIVES, snapshot baseline = 0',
     {SNP: 'search', 'before': 0, 'after': 0, 'delta': 0, ALLOW: 0,
      TGT: '9088145987535878', 'exists_before': True, 'exists_after': True})

show('3) BASELINE OK, delta within allowance',
     {SNP: 'search', 'before': 100, 'after': 100, 'delta': 0, ALLOW: 0,
      TGT: None, 'exists_before': None, 'exists_after': None})

show('4) BASELINE OK, delta explodes',
     {SNP: 'search', 'before': 100, 'after': 0, 'delta': 100, ALLOW: 0,
      TGT: None, 'exists_before': None, 'exists_after': None})

show('5) NO EVIDENCE (probe failed + baseline 0)',
     {SNP: 'search', 'before': 0, 'after': 0, 'delta': 0, ALLOW: 0,
      TGT: '9088145987535878', 'exists_before': False, 'exists_after': False})

show('6) NO IMPACT PAYLOAD', None)
