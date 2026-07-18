"""SQL helper for test-supervisor.ps1.

Usage:
  python _test-sql.py query "SELECT ..."
  python _test-sql.py exec "UPDATE ..."

Outputs one row per line as "col1=col2 col3=col4" pairs.
"""
import sqlite3
import sys
from pathlib import Path

db = str(Path.home() / ".hermes-orchestrator" / "hermes-orch.db")
op = sys.argv[1] if len(sys.argv) > 1 else "query"
sql = sys.argv[2] if len(sys.argv) > 2 else "SELECT 1"

con = sqlite3.connect(db)
con.row_factory = sqlite3.Row

if op == "exec":
    cur = con.execute(sql)
    con.commit()
    print(f"rows affected: {cur.rowcount}")
else:
    for r in con.execute(sql).fetchall():
        print(dict(r))
