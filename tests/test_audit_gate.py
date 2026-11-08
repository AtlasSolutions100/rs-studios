"""tools/audit_gate.py: severity lookup (GitHub advisory rating, CVSS v3 fallback,
aliases) and the gate (fail only on High or Critical). OSV is faked.

    python tests/test_audit_gate.py
"""
import importlib.util
import json
import os
import shutil
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("audit_gate", os.path.join(os.path.dirname(HERE), "tools", "audit_gate.py"))
ag = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ag)

checks = 0


def check(cond, what):
    global checks
    assert cond, what
    checks += 1
    print(f"  ok  {what}")


check(ag.cvss3_score("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H") == 9.8, "CVSS 3.1 critical vector scores 9.8")
check(ag.cvss3_score("CVSS:3.1/AV:L/AC:H/PR:H/UI:R/S:U/C:H/I:H/A:N") == 5.6, "CVSS 3.1 local vector scores 5.6")
check(ag.cvss3_score("CVSS:3.1/AV:N/AC:L/PR:N/UI:R/S:C/C:L/I:L/A:N") == 6.1, "scope-changed vector scores 6.1")
check(ag.cvss3_score("CVSS:3.0/AV:N/AC:L/PR:N/UI:N/S:U/C:N/I:N/A:H") == 7.5, "CVSS 3.0 availability vector scores 7.5")
check(ag.cvss3_score("CVSS:4.0/AV:N/AC:L/AT:N/PR:N/UI:N/VC:H/VI:H/VA:H/SC:N/SI:N/SA:N") is None, "a CVSS 4 vector is not guessed")

OSV = {
    "PYSEC-1": {"aliases": ["GHSA-high"]},
    "GHSA-high": {"database_specific": {"severity": "HIGH"}},
    "GHSA-mod": {"database_specific": {"severity": "MODERATE"}},
    "PYSEC-2": {"severity": [{"type": "CVSS_V3", "score": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"}]},
    "PYSEC-3": {},
}
fetch = OSV.get
check(ag.severity("PYSEC-1", ["GHSA-high", "CVE-1"], fetch) == "HIGH", "the GitHub advisory rating comes through an alias")
check(ag.severity("GHSA-mod", [], fetch) == "MODERATE", "a moderate advisory is moderate")
check(ag.severity("PYSEC-2", [], fetch) == "CRITICAL", "a CVSS-only record is rated from its score")
check(ag.severity("PYSEC-3", ["CVE-x"], fetch) == "unknown", "no rating anywhere is unknown")


def report(*vulns):
    return {"dependencies": [{"name": "pkg", "version": "1.0", "vulns": [
        {"id": v, "aliases": OSV.get(v, {}).get("aliases", []), "fix_versions": ["1.1"]} for v in vulns]},
        {"name": "clean", "version": "2.0", "vulns": []}]}


rows, fail = ag.gate(report("GHSA-mod", "PYSEC-3"), fetch)
check(not fail and len(rows) == 2, "moderate and unknown are reported but do not fail")
rows, fail = ag.gate(report("GHSA-mod", "PYSEC-1"), fetch)
check(fail and rows[0][3] == "HIGH", "a high finding fails, listed first")
rows, fail = ag.gate(report(), fetch)
check(not fail and rows == [], "no findings pass")

tmp = tempfile.mkdtemp()
p = os.path.join(tmp, "a.json")
json.dump(report("GHSA-mod"), open(p, "w"))
ag.fetch_osv = fetch
summary = os.path.join(tmp, "summary.md")
os.environ["GITHUB_STEP_SUMMARY"] = summary
check(ag.main([p]) == 0, "main exits 0 on a moderate finding")
check("MODERATE" in open(summary).read(), "the step summary lists it")
json.dump(report("PYSEC-1"), open(p, "w"))
check(ag.main([p]) == 1, "main exits 1 on a high finding")
open(p, "w").write("not json")
check(ag.main([p]) == 2, "main exits 2 on an unreadable report")
shutil.rmtree(tmp, ignore_errors=True)
print(f"\nall {checks} checks passed")
