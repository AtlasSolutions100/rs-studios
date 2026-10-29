"""Guard against copying the marketing playbooks word for word (see THIRD_PARTY_NOTICES.md).

A "sentence" is a run of text between full stops, question marks, exclamation marks, line breaks or table bars.
For every playbook sentence longer than 12 words we keep a fingerprint of its first 13 words (a 48-bit hash of the
lower-cased words), never the text. The check hashes every 13-word window of Campaignify's own files and fails on
any match, so a copied sentence (or the first 13 words of one) can't slip in.

Refresh the fingerprints after updating the reference copy of the playbooks:

    python3 tests/playbook_guard.py /path/to/marketingskills/skills > tests/playbook_fingerprints.txt
"""
import hashlib
import os
import re
import sys

N = 13
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FINGERPRINTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "playbook_fingerprints.txt")


def words(text: str) -> list[str]:
    text = text.replace("\\n", " ").replace("’", "'").lower()
    return re.findall(r"[a-z0-9]+", text)


def _h(ws: list[str]) -> str:
    return hashlib.blake2b(" ".join(ws).encode(), digest_size=6).hexdigest()


def sentences(text: str) -> list[list[str]]:
    out = []
    for part in re.split(r"[.!?]+(?=\s|$)|\n|\|", text):
        ws = words(part)
        if len(ws) > N - 1:
            out.append(ws)
    return out


def fingerprints(skills_dir: str) -> set[str]:
    fp = set()
    for base, _, files in os.walk(skills_dir):
        for f in files:
            if f.endswith(".md"):
                with open(os.path.join(base, f), encoding="utf-8", errors="replace") as fh:
                    for ws in sentences(fh.read()):
                        fp.add(_h(ws[:N]))
    return fp


def stored() -> set[str]:
    with open(FINGERPRINTS) as fh:
        return {ln.strip() for ln in fh if ln.strip() and not ln.startswith("#")}


def our_files() -> list[str]:
    """Campaignify's prompts and templates: the Python modules, the page templates and the scripts."""
    out = [os.path.join(ROOT, f) for f in os.listdir(ROOT) if f.endswith(".py")]
    out += [os.path.join(ROOT, "web", f) for f in os.listdir(os.path.join(ROOT, "web")) if f.endswith(".html")]
    out += [os.path.join(ROOT, "static", f) for f in os.listdir(os.path.join(ROOT, "static")) if f.endswith(".js")]
    return sorted(out)


def copied(fp: set[str], paths: list[str] | None = None) -> list[tuple[str, str]]:
    """[(file, the matching 13 words)] for every window of our files whose fingerprint is in `fp`."""
    hits = []
    for path in paths or our_files():
        with open(path, encoding="utf-8", errors="replace") as fh:
            ws = words(fh.read())
        for i in range(len(ws) - N + 1):
            if _h(ws[i:i + N]) in fp:
                hits.append((os.path.relpath(path, ROOT), " ".join(ws[i:i + N])))
    return hits


if __name__ == "__main__":
    fp = fingerprints(sys.argv[1])
    print(f"# {len(fp)} fingerprints: first {N} words of each playbook sentence longer than {N - 1} words "
          f"(blake2b, 48 bits). Made by tests/playbook_guard.py; no playbook text is stored here.")
    for h in sorted(fp):
        print(h)
