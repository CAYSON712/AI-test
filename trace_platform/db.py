# -*- coding: utf-8 -*-
"""
SQLite 数据库初始化（自建 trace 平台）
对应 Langfuse 数据模型：trace / observation / score
"""
import sqlite3
import os
import sys

# Windows 终端默认 GBK，无法输出 emoji，需强制 UTF-8
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

DB_PATH = os.path.join(os.path.dirname(__file__), "trace_platform.db")


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    """建表：traces / observations / scores / datasets"""
    conn = get_conn()
    c = conn.cursor()

    # 一次完整对话（Trace）
    c.execute("""
        CREATE TABLE IF NOT EXISTS traces (
            id TEXT PRIMARY KEY,
            name TEXT,
            input TEXT,
            output TEXT,
            metadata TEXT,           -- JSON
            timestamp TEXT
        )
    """)

    # trace 里的节点（Observation）
    c.execute("""
        CREATE TABLE IF NOT EXISTS observations (
            id TEXT PRIMARY KEY,
            trace_id TEXT NOT NULL,
            name TEXT,
            type TEXT,               -- GENERATION / SPAN
            input TEXT,
            output TEXT,
            parent_id TEXT,          -- 父节点 id，null=root
            start_time TEXT,
            end_time TEXT,
            metadata TEXT,
            FOREIGN KEY (trace_id) REFERENCES traces(id),
            FOREIGN KEY (parent_id) REFERENCES observations(id)
        )
    """)

    # 打分（Score）
    c.execute("""
        CREATE TABLE IF NOT EXISTS scores (
            id TEXT PRIMARY KEY,
            trace_id TEXT,
            observation_id TEXT,
            name TEXT,               -- 维度名
            value TEXT,              -- 数值/布尔/分类/文本
            data_type TEXT,          -- NUMERIC/BOOLEAN/CATEGORICAL/TEXT
            comment TEXT,
            metadata TEXT,           -- 评分来源(via)/理由(detail) 等 JSON
            FOREIGN KEY (trace_id) REFERENCES traces(id)
        )
    """)
    # 兼容旧库：scores 表若缺 metadata 列则补上
    _cols = [r[1] for r in c.execute("PRAGMA table_info(scores)")]
    if "metadata" not in _cols:
        c.execute("ALTER TABLE scores ADD COLUMN metadata TEXT")
        conn.commit()

    # 测试数据集（Dataset）
    c.execute("""
        CREATE TABLE IF NOT EXISTS datasets (
            id TEXT PRIMARY KEY,
            name TEXT,
            description TEXT
        )
    """)

    conn.commit()
    conn.close()
    print(f"✅ 数据库初始化完成: {DB_PATH}")


if __name__ == "__main__":
    init_db()
