# -*- coding: utf-8 -*-
"""清空 trace_platform 的所有 trace 数据（traces/observations/scores 三表）。
先备份原 db，再清空。只清 trace 相关数据，保留库结构。
"""
import sys, os, shutil, sqlite3
sys.stdout.reconfigure(encoding="utf-8")

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trace_platform.db")

# 1. 备份
bak = DB + ".bak"
if os.path.exists(bak):
    os.remove(bak)
shutil.copy2(DB, bak)
print(f"已备份: {bak}")

# 2. 清空（注意外键：先删子表，再删主表）
conn = sqlite3.connect(DB)
c = conn.cursor()
before = {}
for t in ("observations", "scores", "traces"):
    before[t] = c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]

# 关闭外键约束以简化删除顺序（或用正确顺序）
conn.execute("PRAGMA foreign_keys = OFF")
c.execute("DELETE FROM observations")
c.execute("DELETE FROM scores")
c.execute("DELETE FROM traces")
conn.commit()
conn.close()

for t in ("observations", "scores", "traces"):
    print(f"{t}: {before[t]} → 0")
print("✅ trace 数据已清空（库结构保留，可继续上报）")
