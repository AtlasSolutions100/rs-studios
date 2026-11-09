"""Run every test suite, each in its own process, and report them all (roadmap M7.6, observation 18).

    python tests/test_app.py                 # what CI and deploy.yml run: every suite (this runner)
    python tests/run_all.py                  # the same
    python tests/run_all.py insights manage  # only these (names with or without test_ and .py)
    python tests/run_all.py --list           # the suites, in order, and nothing run

A failing suite no longer stops the chain: the next one still runs, and the end of the output lists every suite
with its result, its check count and its time, then the failures with their last lines. The exit code is 1 when
any suite failed (or timed out), 0 when all passed.

Order: tests/test_app.py's own checks first, then the suites in the order test_app.py's `__main__` block names
them (each line there says what the suite covers), then any tests/test_*.py file that block doesn't name yet, so
a new suite runs even before someone adds its line. Each suite gets RS_TEST_ONE=1, which keeps test_app.py from
starting this runner again.

RS_TEST_TIMEOUT (seconds, default 1200) stops one suite that hangs; it counts as failed and the rest still run.
In GitHub Actions the table also goes to the job summary (GITHUB_STEP_SUMMARY).
"""
import os
import re
import subprocess
import sys
import threading
import time
from collections import deque

HERE = os.path.dirname(os.path.abspath(__file__))
MAIN = "test_app.py"
PASSED_RE = re.compile(r"(\d[\d,]*)\s+(?:[\w-]+\s+){0,3}checks? passed")
TAIL_LINES = 25


def listed_order() -> list:
    """The suites test_app.py's __main__ block names, in its order."""
    try:
        src = open(os.path.join(HERE, MAIN), encoding="utf-8").read()
    except OSError:
        return []
    i = src.find('if __name__ == "__main__"', src.find("checks passed"))
    names = re.findall(r'HERE,\s*"(test_[A-Za-z0-9_]+\.py)"', src[i:] if i >= 0 else "")
    return list(dict.fromkeys(names))


def suites() -> list:
    """[(file name, listed in test_app.py?)], test_app.py first."""
    have = {f for f in os.listdir(HERE) if f.startswith("test_") and f.endswith(".py")}
    order = [MAIN] if MAIN in have else []
    listed = [f for f in listed_order() if f in have and f != MAIN]
    order += listed
    extra = sorted(have - set(order))
    return [(f, f == MAIN or f in listed) for f in order] + [(f, False) for f in extra]


def _norm(name: str) -> str:
    n = os.path.basename(name.strip())
    if not n.endswith(".py"):
        n += ".py"
    return n if n.startswith("test_") else "test_" + n


def run_one(name: str, timeout: float) -> dict:
    """Run one suite, streaming its output; returns its result."""
    env = dict(os.environ, RS_TEST_ONE="1", PYTHONUNBUFFERED="1")
    t0 = time.time()
    tail = deque(maxlen=TAIL_LINES)
    checks = None
    print(f"\n==== {name}", flush=True)
    p = subprocess.Popen([sys.executable, os.path.join(HERE, name)], cwd=os.path.dirname(HERE), env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace", bufsize=1)
    killed = threading.Event()

    def _stop():                    # a suite that hangs, even one that prints nothing, is stopped here
        killed.set()
        p.kill()
    timer = threading.Timer(timeout, _stop)
    timer.daemon = True
    timer.start()
    try:
        for line in p.stdout:
            sys.stdout.write(line)
            tail.append(line.rstrip("\n"))
            m = PASSED_RE.search(line)
            if m:
                checks = int(m.group(1).replace(",", ""))
        code = p.wait()
    finally:
        timer.cancel()
        p.stdout.close()
    sys.stdout.flush()
    timed_out = killed.is_set()
    ok = code == 0 and not timed_out
    return {"name": name, "ok": ok, "code": code, "timed_out": timed_out, "checks": checks if ok else None,
            "seconds": round(time.time() - t0, 1), "tail": list(tail)}


def _why(r: dict) -> str:
    if r["timed_out"]:
        return f"timed out after {r['seconds']:g} s"
    fail = next((ln for ln in reversed(r["tail"]) if ln.startswith("FAIL")), None)
    if fail:
        return fail
    last = next((ln for ln in reversed(r["tail"]) if ln.strip()), "")
    return f"exit {r['code']}: {last[:200]}"


def report(results: list, seconds: float, unlisted: list) -> str:
    bad = [r for r in results if not r["ok"]]
    total = sum(r["checks"] or 0 for r in results)
    lines = ["", "=" * 72,
             f"{len(results)} suites: {len(results) - len(bad)} passed, {len(bad)} failed; "
             f"{total:,} checks passed; {seconds:.0f} s", "=" * 72]
    for r in results:
        mark = "ok  " if r["ok"] else "FAIL"
        n = f"{r['checks']:,} checks" if r["checks"] is not None else ("" if not r["ok"] else "passed")
        lines.append(f"  {mark} {r['name']:<34} {n:>14}  {r['seconds']:>6.1f} s")
    for r in bad:
        lines += ["", f"---- {r['name']} failed: {_why(r)}"] + [f"   | {ln}" for ln in r["tail"][-12:]]
    if unlisted:
        lines += ["", "Not named in test_app.py's list yet (ran anyway): " + ", ".join(unlisted)]
    return "\n".join(lines)


def _step_summary(results: list, seconds: float):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    bad = [r for r in results if not r["ok"]]
    rows = ["| suite | result | checks | seconds |", "|---|---|---:|---:|"]
    for r in results:
        rows.append(f"| {r['name']} | {'passed' if r['ok'] else 'FAILED'} | {r['checks'] or ''} | {r['seconds']:g} |")
    head = (f"### Tests: {len(results) - len(bad)} of {len(results)} suites passed "
            f"({sum(r['checks'] or 0 for r in results):,} checks, {seconds:.0f} s)\n")
    fails = "".join(f"\n**{r['name']}**: `{_why(r)[:300]}`\n" for r in bad)
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(head + "\n" + "\n".join(rows) + "\n" + fails)
    except OSError:
        pass


def main(argv: list) -> int:
    all_suites = suites()
    if "--list" in argv:
        for name, listed in all_suites:
            print(name + ("" if listed else "   (not named in test_app.py yet)"))
        return 0
    want = [_norm(a) for a in argv if not a.startswith("-")]
    if want:
        known = {n for n, _ in all_suites}
        missing = [w for w in want if w not in known]
        if missing:
            print("no such suite: " + ", ".join(missing))
            return 2
        picked = [(n, listed) for n, listed in all_suites if n in want]
    else:
        picked = all_suites
    try:
        timeout = float(os.environ.get("RS_TEST_TIMEOUT") or 1200)
    except ValueError:
        timeout = 1200.0
    t0 = time.time()
    results = [run_one(n, timeout) for n, _ in picked]
    seconds = time.time() - t0
    print(report(results, seconds, [n for n, listed in picked if not listed]), flush=True)
    _step_summary(results, seconds)
    return 0 if all(r["ok"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
