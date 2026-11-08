"""Gate on pip-audit's findings: report all of them, fail only on High or Critical.

    pip-audit -r requirements.txt --disable-pip --require-hashes --format json -o audit.json || true
    python tools/audit_gate.py audit.json

pip-audit lists known vulnerabilities but not how bad they are. For each one this
looks up the OSV record (https://api.osv.dev/v1/vulns/<id>) and its aliases: the
GitHub advisory's rating (LOW / MODERATE / HIGH / CRITICAL) when there is one,
else the CVSS v3 base score from the vector. A finding whose severity can't be
found is reported as "unknown" and does not fail the run (it is listed so a
person looks at it).

Exit 0: nothing High or Critical. Exit 1: at least one. Exit 2: the report is
missing or unreadable (so a broken audit step never passes silently).
Writes a Markdown table to $GITHUB_STEP_SUMMARY when that is set.
"""
import json
import math
import os
import sys
import urllib.request

OSV = "https://api.osv.dev/v1/vulns/"
FAIL_ON = {"HIGH", "CRITICAL"}
RANK = {"unknown": 0, "LOW": 1, "MODERATE": 2, "HIGH": 3, "CRITICAL": 4}


def fetch_osv(vid: str) -> dict | None:
    try:
        with urllib.request.urlopen(OSV + vid, timeout=20) as r:
            return json.load(r)
    except Exception:  # noqa: BLE001: a missing record just means "look at the next alias"
        return None


def _roundup(x: float) -> float:
    i = round(x * 100000)
    return i / 100000.0 if i % 10000 == 0 else (math.floor(i / 10000) + 1) / 10.0


def cvss3_score(vector: str) -> float | None:
    """CVSS v3.0 / v3.1 base score from a vector string, or None if it isn't one."""
    if not vector.startswith("CVSS:3"):
        return None
    try:
        m = dict(p.split(":", 1) for p in vector.split("/")[1:])
        av = {"N": 0.85, "A": 0.62, "L": 0.55, "P": 0.2}[m["AV"]]
        ac = {"L": 0.77, "H": 0.44}[m["AC"]]
        ui = {"N": 0.85, "R": 0.62}[m["UI"]]
        changed = m["S"] == "C"
        pr = {"N": 0.85, "L": 0.68 if changed else 0.62, "H": 0.5 if changed else 0.27}[m["PR"]]
        cia = {"H": 0.56, "L": 0.22, "N": 0.0}
        iss = 1 - (1 - cia[m["C"]]) * (1 - cia[m["I"]]) * (1 - cia[m["A"]])
    except (KeyError, ValueError):
        return None
    impact = 7.52 * (iss - 0.029) - 3.25 * (iss - 0.02) ** 15 if changed else 6.42 * iss
    if impact <= 0:
        return 0.0
    expl = 8.22 * av * ac * pr * ui
    return _roundup(min(1.08 * (impact + expl), 10)) if changed else _roundup(min(impact + expl, 10))


def rating(score: float) -> str:
    return "CRITICAL" if score >= 9 else "HIGH" if score >= 7 else "MODERATE" if score >= 4 else "LOW"


def severity(vid: str, aliases: list[str], fetch=None) -> str:
    fetch = fetch or fetch_osv
    best_cvss = None
    for i in [vid] + [a for a in aliases if a != vid]:
        rec = fetch(i)
        if not rec:
            continue
        s = ((rec.get("database_specific") or {}).get("severity") or "").upper()
        if s in RANK and s != "unknown":
            return s
        for sev in rec.get("severity") or []:
            sc = cvss3_score(sev.get("score") or "")
            if sc is not None:
                best_cvss = max(best_cvss or 0.0, sc)
    return rating(best_cvss) if best_cvss is not None else "unknown"


def gate(report: dict, fetch=None) -> tuple[list[tuple], bool]:
    rows = []
    for dep in report.get("dependencies", []):
        for v in dep.get("vulns", []):
            sev = severity(v["id"], v.get("aliases") or [], fetch)
            rows.append((dep["name"], dep.get("version", "?"), v["id"], sev, ", ".join(v.get("fix_versions") or []) or "none"))
    rows.sort(key=lambda r: -RANK[r[3]])
    return rows, any(r[3] in FAIL_ON for r in rows)


def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    try:
        report = json.load(open(argv[0], encoding="utf-8"))
        n = len(report["dependencies"])
    except Exception as e:  # noqa: BLE001
        print(f"audit_gate: can't read the pip-audit report ({e})")
        return 2
    rows, fail = gate(report)
    head = (f"pip-audit: {n} packages, {len(rows)} known vulnerabilities"
            + (f"; {sum(r[3] in FAIL_ON for r in rows)} High or Critical" if rows else ""))
    print(head)
    for r in rows:
        print(f"  {r[3]:<9} {r[0]}=={r[1]}  {r[2]}  fixed in: {r[4]}")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(f"### {head}\n\n")
            if rows:
                f.write("| Severity | Package | Advisory | Fixed in |\n|---|---|---|---|\n")
                for r in rows:
                    f.write(f"| {r[3]} | {r[0]}=={r[1]} | {r[2]} | {r[4]} |\n")
            f.write("\nOnly High and Critical fail this check; the rest are listed to plan upgrades.\n")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
