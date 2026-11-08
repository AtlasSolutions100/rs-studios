"""Migrations stay additive (N3), on throwaway databases.

The deploy hook's side container runs db.init() against the live database before
the switch, and a rollback puts back only the old image. So the old code has to
keep working on the new schema: migrations may add tables and columns and fill
new columns, never drop, rename or retype a column, or drop a table.

  * every column in tests/schema_columns.txt (the schema as of this file) is still
    there, with the same declared type, in a fresh database: a dropped, renamed or
    retyped column fails here. New tables and columns are fine (run with --update
    to add them to the list);
  * an old database (the base SCHEMA, before any COLUMNS) upgrades in place: its
    rows survive, every column is added, a copy is taken first, and a second
    init() changes nothing;
  * no app module has DROP TABLE, DROP COLUMN, RENAME or ALTER TABLE other than
    db.init's ADD COLUMN.

    python tests/test_migrations.py            (tests/test_app.py runs it too)
    python tests/test_migrations.py --update   rewrite schema_columns.txt after adding a column
"""
import glob
import os
import re
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp(prefix="rs_migrations_")
os.environ.update(RS_DATA=TMP, RS_WORKER="0", RS_PLAN_SYNC="0", RS_NUDGE_LOOP="0")

import db  # noqa: E402

SNAPSHOT = os.path.join(HERE, "schema_columns.txt")
passed = 0


def check(cond, msg):
    global passed
    if not cond:
        raise SystemExit(f"FAIL: {msg}")
    passed += 1
    print("  ok ", msg)


def columns(path) -> dict:
    """{"table.column": "declared type"} for every table in the database at path."""
    out = {}
    with sqlite3.connect(path) as conn:
        for (t,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
            for row in conn.execute(f"PRAGMA table_info({t})"):
                out[f"{t}.{row[1]}"] = (row[2] or "").upper()
    return out


def read_snapshot() -> dict:
    out = {}
    with open(SNAPSHOT) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                name, _, typ = line.partition(" ")
                out[name] = typ.strip()
    return out


print("== a fresh database keeps every column the app has had")
db.init()
fresh = columns(db.DB_PATH)
if "--update" in sys.argv:
    keep = read_snapshot() if os.path.exists(SNAPSHOT) else {}
    gone = sorted(set(keep) - set(fresh))
    if gone:
        raise SystemExit(f"FAIL: these columns are gone, so the list can't be updated: {', '.join(gone)}")
    with open(SNAPSHOT, "w") as f:
        f.write("# Every table.column (declared type) of a fresh database. tests/test_migrations.py fails if one\n"
                "# disappears or changes type: migrations are additive only (db.init). Refresh after adding a\n"
                "# column with: python tests/test_migrations.py --update\n")
        for name in sorted(fresh):
            f.write(f"{name} {fresh[name]}\n")
    print(f"wrote {len(fresh)} columns to {SNAPSHOT}")
    sys.exit(0)
known = read_snapshot()
missing = sorted(set(known) - set(fresh))
check(not missing, "no column was dropped or renamed" + (f" (missing: {', '.join(missing)})" if missing else ""))
retyped = sorted(k for k in known if k in fresh and fresh[k] != known[k])
check(not retyped, "no column changed its declared type" + (f" ({', '.join(retyped)})" if retyped else ""))
tables = {k.split(".")[0] for k in known}
check(tables <= {k.split(".")[0] for k in fresh}, "no table was dropped")
new = sorted(set(fresh) - set(known))
if new:
    print(f"  note: {len(new)} new column(s) not in schema_columns.txt yet: {', '.join(new[:8])}"
          + (" ..." if len(new) > 8 else "") + " (run with --update)")

print("== an old database upgrades in place")
old_dir = tempfile.mkdtemp(prefix="old_", dir=TMP)
old_path = os.path.join(old_dir, "rs.db")
with sqlite3.connect(old_path) as conn:
    conn.executescript(db.SCHEMA)   # the base tables, before the COLUMNS migrations
    base_cols = {r[1] for r in conn.execute("PRAGMA table_info(users)")}
    conn.execute("INSERT INTO workspaces (name, credits, created_at) VALUES ('Old Co', 42, '2025-01-01T00:00:00+00:00')")
    conn.execute("INSERT INTO users (email, name, pw_hash, is_admin, workspace_id, created_at) "
                 "VALUES ('old@example.com', 'Old', 'hash-kept', 0, 1, '2025-01-01T00:00:00+00:00')")
    conn.commit()
added_to_users = [c for t, c, _ in db.COLUMNS if t == "users" and c not in base_cols]
check(added_to_users, "(the old users table lacks the newer columns)")
_real = db.DB_PATH
db.DB_PATH = old_path
try:
    db.init()
    after = columns(old_path)
    with sqlite3.connect(old_path) as conn:
        conn.row_factory = sqlite3.Row
        u = conn.execute("SELECT * FROM users WHERE email='old@example.com'").fetchone()
        ws = conn.execute("SELECT * FROM workspaces WHERE id=1").fetchone()
    check(u["pw_hash"] == "hash-kept" and u["name"] == "Old" and ws["credits"] == 42 and ws["name"] == "Old Co",
          "the old rows keep their values")
    check(u["email_verified_at"] and u["role"] == "owner" and u["session_version"] == 0 and u["signup_pending"] == 0,
          "new columns get their backfill or default (an old account is verified, owns its workspace, isn't waiting)")
    check(all(f"{t}.{c}" in after for t, c, _ in db.COLUMNS), "every COLUMNS entry was added")
    check(set(fresh) <= set(after), "the upgraded database has every column a fresh one has")
    check(glob.glob(old_path + ".pre-schema-*"), "a copy was taken before the schema changed")
    db.init()
    check(columns(old_path) == after, "a second init() changes nothing")
finally:
    db.DB_PATH = _real

print("== no destructive DDL in the app")
bad = []
pattern = re.compile(r"\b(DROP\s+TABLE|DROP\s+COLUMN|RENAME\s+(TO|COLUMN)|ALTER\s+TABLE)\b", re.I)
for path in sorted(glob.glob(os.path.join(ROOT, "*.py")) + glob.glob(os.path.join(ROOT, "tools", "*.py"))):
    with open(path, encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            m = pattern.search(line)
            if m and not (os.path.basename(path) == "db.py" and "ADD COLUMN" in line):
                bad.append(f"{os.path.relpath(path, ROOT)}:{n}")
check(not bad, "no DROP TABLE / DROP COLUMN / RENAME / other ALTER TABLE outside db.init's ADD COLUMN"
      + (f" (found: {', '.join(bad)})" if bad else ""))
dup = [k for k in {(t, c) for t, c, _ in db.COLUMNS} if sum(1 for t, c, _ in db.COLUMNS if (t, c) == k) > 1]
check(not dup, "each COLUMNS entry is listed once")

print(f"\nall {passed} migration checks passed ({TMP})")
