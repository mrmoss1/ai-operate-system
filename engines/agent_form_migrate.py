#!/usr/bin/env python3
"""agent_form_migrate.py — flat-file -> six-folder agent FORM migration (t0089 second wave).

ZONE 1 — SHAREABLE SYSTEM. Owned by the Agent Fleet Triage agent.

An agent is "six-folder" only when each component is a FOLDER with index.html
(03-process/index.html), not a flat file (03-process.html). A flat component
cannot hold a co-located engine (03-process/foo.py) — so flat agents block the
Rule 1.6 owner-agent model. This engine converts NN-name.html -> NN-name/index.html
(pure rename, Rule 15.5a — no delete grant), rewrites any intra-agent relative
links for the new depth, and VALIDATES that every link still resolves.

CLI:
  python3 agent_form_migrate.py --scan [root]              # detector: list flat agents
  python3 agent_form_migrate.py --migrate <agent_dir> [--dry-run]
  python3 agent_form_migrate.py --self-test
"""
from __future__ import annotations
import os, re, sys, posixpath
from pathlib import Path

COMP_RE = re.compile(r"^(0[1-6]-[A-Za-z0-9][A-Za-z0-9._-]*)\.html$")
HREF_RE = re.compile(r"""(href|src)=("|')([^"']+)(\2)""")
SKIP_PARTS = {".git", "__pycache__", "node_modules", "_archive", "outputs", "uploads"}
# Content that matches 0[1-6]-*.html but is NOT an agent component -- golden-example
# files, swarm variants, site/field-notes. Guards --scan/--migrate against false
# positives that would corrupt content if converted (t0617).
SCAN_EXCLUDE = {"02-golden-example", "variants", "site", "field-notes"}

def _skip(p: Path) -> bool:
    return any(part in SKIP_PARTS for part in p.parts)

def find_flat_components(agent: Path):
    out = []
    for p in sorted(agent.iterdir()):
        if p.is_file():
            m = COMP_RE.match(p.name)
            if m:
                out.append((m.group(1), p))
    return out

def is_agent_dir(d: Path) -> bool:
    # heuristic: holds at least one NN-name.html or NN-name/ component
    for p in d.iterdir():
        if p.is_file() and COMP_RE.match(p.name):
            return True
        if p.is_dir() and re.match(r"^0[1-6]-", p.name):
            return True
    return False

def scan(root: Path):
    flat = []
    for d in root.rglob("*"):
        if not d.is_dir() or _skip(d.relative_to(root)):
            continue
        if "Agents" not in [pp for pp in d.parts] and "agents" not in [pp.lower() for pp in d.parts]:
            continue
        if d.name.lower() in ("agents",) or "runs" in [pp.lower() for pp in d.parts]:
            continue
        if any(part in SCAN_EXCLUDE for part in d.relative_to(root).parts):
            continue  # golden-example / variants / site content, not agent components
        comps = find_flat_components(d)
        if comps:
            flat.append((str(d.relative_to(root)), [s for s, _ in comps]))
    return sorted(flat)

def _is_external(h: str) -> bool:
    return (h.startswith(("http://", "https://", "//", "/", "#", "mailto:",
            "computer://", "data:", "file:", "tel:")))

def _rewrite(text: str, old_dir: str, new_dir: str, converted: set) -> str:
    def repl(m):
        attr, q, h, _ = m.groups()
        if _is_external(h):
            return m.group(0)
        hp, frag = (h.split("#", 1) + [""])[:2]
        if not hp:
            return m.group(0)
        tgt = posixpath.normpath(posixpath.join(old_dir, hp))
        base = posixpath.basename(tgt)
        cm = COMP_RE.match(base)
        if cm and cm.group(1) in converted:
            tgt = posixpath.join(posixpath.dirname(tgt), cm.group(1), "index.html")
        start = new_dir if new_dir else "."
        rel = posixpath.relpath(tgt, start)
        return "%s=%s%s%s%s" % (attr, q, rel, ("#" + frag) if frag else "", q)
    return HREF_RE.sub(repl, text)

def _html_files(agent: Path):
    return [p for p in agent.rglob("*.html") if not _skip(p.relative_to(agent))]

def validate_links(agent: Path):
    """Return list of (file, href) for intra-agent relative links that dangle."""
    dangling = []
    for f in _html_files(agent):
        try:
            txt = f.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for m in HREF_RE.finditer(txt):
            h = m.group(3)
            if _is_external(h):
                continue
            hp = h.split("#", 1)[0]
            if not hp:
                continue
            target = (f.parent / hp).resolve()
            if not target.exists():
                dangling.append((str(f.relative_to(agent)), h))
    return dangling

def migrate_agent(agent: Path, dry_run=False):
    agent = Path(agent)
    if any(part in SCAN_EXCLUDE for part in agent.parts):
        return {"agent": str(agent), "error": "refused: path contains a content segment "
                "(02-golden-example/variants/site/field-notes), not an agent root"}
    flat = find_flat_components(agent)
    if not flat:
        return {"agent": str(agent), "converted": [], "note": "already six-folder (no flat components)"}
    converted = {s for s, _ in flat}
    # guard: NN-name.html AND NN-name/ both exist -> conflict, skip that one
    conflicts = [s for s in converted if (agent / s).is_dir()]
    if conflicts:
        return {"agent": str(agent), "error": "conflict (file+folder both exist): " + ", ".join(conflicts)}
    if dry_run:
        return {"agent": str(agent), "would_convert": sorted(converted)}
    sys.path.insert(0, str(_find_cos(agent)))
    sys.path.insert(0, __import__('os').path.join(str(_find_cos(agent)), "Agents", "cos-platform", "03-process"))  # t0560 engine home
    from atomic_write import atomic_write_file, validate_ends_with
    # 1) gather planned new-dir for every html file (agent-relative posix dir)
    plans = []  # (path, old_dir, new_dir)
    for f in _html_files(agent):
        rel = f.relative_to(agent).as_posix()
        m = COMP_RE.match(rel)  # top-level flat component?
        if m and m.group(1) in converted:
            plans.append((f, "", m.group(1)))  # moves from root into slug/
        else:
            d = posixpath.dirname(rel)
            plans.append((f, d, d))
    # 2) rewrite + write each (move first for converted)
    for f, old_dir, new_dir in plans:
        txt = f.read_text(encoding="utf-8", errors="ignore")
        new_txt = _rewrite(txt, old_dir, new_dir, converted)
        if old_dir != new_dir:  # a moved flat component
            dest_dir = agent / new_dir
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / "index.html"
            os.replace(str(f), str(dest))  # rename, no delete grant
            tail = b"</html>" if new_txt.rstrip().endswith("</html>") else None
            atomic_write_file(str(dest), new_txt.encode("utf-8"),
                              validate=validate_ends_with(tail) if tail else None)
        elif new_txt != txt:
            tail = b"</html>" if new_txt.rstrip().endswith("</html>") else None
            atomic_write_file(str(f), new_txt.encode("utf-8"),
                              validate=validate_ends_with(tail) if tail else None)
    dangling = validate_links(agent)
    return {"agent": str(agent), "converted": sorted(converted), "dangling": dangling}

def _find_cos(start: Path):
    for anc in [start] + list(start.parents):
        if (anc / "0. Chief of Staff" / "atomic_write.py").exists():
            return anc / "0. Chief of Staff"
    return start

def self_test():
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cos = root / "0. Chief of Staff"; cos.mkdir()
        # stub atomic_write so the engine can import it
        (cos / "atomic_write.py").write_text(
            "def atomic_write_file(p,b,validate=None):\n"
            "    import os\n"
            "    open(p,'wb').write(b)\n"
            "def validate_ends_with(s):\n    return lambda b: None\n")
        agent = root / "2. Products" / "Projects" / "x" / "agents" / "link-01"
        agent.mkdir(parents=True)
        (agent / "01-start-here.html").write_text('<html><a href="03-process.html">p</a></html>')
        (agent / "03-process.html").write_text("<html>proc</html>")
        (agent / "02-golden-example").mkdir()
        (agent / "02-golden-example" / "index.html").write_text('<html><a href="../03-process.html">p</a></html>')
        res = migrate_agent(agent)
        assert (agent / "01-start-here" / "index.html").exists(), "01 not converted"
        assert (agent / "03-process" / "index.html").exists(), "03 not converted"
        assert not (agent / "03-process.html").exists(), "old flat file remained"
        assert res["dangling"] == [], "dangling links: %r" % res["dangling"]
        # the 01 link should now point to ../03-process/index.html
        t = (agent / "01-start-here" / "index.html").read_text()
        assert "../03-process/index.html" in t, t
        # the already-folder 02 link should now resolve too
        t2 = (agent / "02-golden-example" / "index.html").read_text()
        assert "../03-process/index.html" in t2, t2
        print("agent_form_migrate self-test: PASS")

if __name__ == "__main__":
    a = sys.argv[1:]
    if "--self-test" in a:
        self_test(); sys.exit(0)
    if "--scan" in a:
        rest = [x for x in a if x != "--scan"]
        root = Path(rest[0]) if rest else Path(__file__).resolve().parents[4]
        for rel, comps in scan(root):
            print("%2d flat  %s  [%s]" % (len(comps), rel, ",".join(comps)))
        sys.exit(0)
    if "--migrate" in a:
        rest = [x for x in a if x not in ("--migrate", "--dry-run")]
        dry = "--dry-run" in a
        import json as _j
        print(_j.dumps(migrate_agent(Path(rest[0]), dry_run=dry), indent=2))
        sys.exit(0)
    print(__doc__)
