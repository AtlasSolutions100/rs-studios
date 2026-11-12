"""The deploy bundle fits what the deploy hook accepts.

deploy.yml packs the repo with `git archive`, base64s it into one JSON body and posts it to the deploy hook on the
server. The hook refuses a body over 64 MiB ("body missing or too large"), and that only shows at deploy time, after
everything is merged (release m35, Nov 12 2026: 36 MB of new example clips and presenter pictures took the bundle
from 40 MB to 75 MB, and main could not be deployed). So this fails on the pull request instead.

Pictures and clips under static/ are nearly all of the bundle. To add more than the room left, make them smaller or
keep them out of the repo; raising the hook's limit is a change on the server, not here.

    python tests/test_bundle_size.py      (tests/test_app.py runs it too)
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
HOOK_MAX_BODY = 64 * 1024 * 1024      # the deploy hook's limit on the JSON body
SETTINGS_ROOM = 256 * 1024            # the settings sent beside the archive, generously
KEEP_FREE = 3 * 1024 * 1024           # room that must stay free, so one more change still deploys

passed = 0


def check(cond, msg):
    global passed
    if not cond:
        raise SystemExit(f"FAIL: {msg}")
    passed += 1
    print("  ok ", msg)


def b64(n: int) -> int:
    return (n + 2) // 3 * 4


try:
    out = subprocess.run(["git", "archive", "--format=tar.gz", "HEAD"], cwd=ROOT, capture_output=True, timeout=300)
    tgz = len(out.stdout) if out.returncode == 0 else None
except (OSError, subprocess.SubprocessError):
    tgz = None

if tgz is None:
    print("  skip  not a git checkout: the bundle cannot be packed here")
else:
    # Files changed but not committed yet are not in HEAD; count what is on disk for them, so a local run before
    # the commit still tells the truth (media does not compress, so its size is what it adds).
    try:
        changed = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all", "--", "static"], cwd=ROOT,
                                 capture_output=True, text=True, timeout=60).stdout.splitlines()
    except (OSError, subprocess.SubprocessError):
        changed = []
    extra = sum(os.path.getsize(os.path.join(ROOT, line[3:].strip().strip('"'))) for line in changed
                if line[:2].strip() in ("??", "A", "AM") and os.path.isfile(os.path.join(ROOT, line[3:].strip().strip('"'))))
    body = b64(tgz + extra) + SETTINGS_ROOM
    mb = 1024 * 1024
    print(f"  bundle {tgz / mb:.1f} MiB (+{extra / mb:.1f} not committed), as a JSON body {body / mb:.1f} of {HOOK_MAX_BODY / mb:.0f} MiB")
    check(body <= HOOK_MAX_BODY, "the deploy bundle fits the deploy hook's 64 MiB body limit")
    check(body <= HOOK_MAX_BODY - KEEP_FREE,
          f"with {KEEP_FREE // mb} MiB to spare (smaller clips and pictures, or keep them out of the repo)")
    attrs = open(os.path.join(ROOT, ".gitattributes"), encoding="utf-8").read()
    import io
    import tarfile
    raw = subprocess.run(["git", "archive", "--format=tar", "HEAD"], cwd=ROOT, capture_output=True, timeout=300).stdout
    names = set(tarfile.open(fileobj=io.BytesIO(raw)).getnames())
    check("tests export-ignore" in attrs and not any(n == "tests" or n.startswith("tests/") for n in names)
          and {"app.py", "presenters.py", "Dockerfile", "static/showcase/manifest.json"} <= names,
          "the tests are left out of the bundle (the server image never had them); the app is in it")

print(f"\nbundle size: {passed} checks passed")
