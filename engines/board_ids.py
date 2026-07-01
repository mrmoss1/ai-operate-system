#!/usr/bin/env python3
"""board_ids.py - globally-unique card id assignment for the pickup-brief board.

Fix for the t0288 next-id reuse bug: nextTId() and ad-hoc programmatic adds
read only the CURRENT day's board; the Daily Rollover drops verdict==done
cards, so their ids vanish from the new board and the counter REUSES them -
minting a second, different card with the same t-id. This module sources the
next id from the GLOBAL max across every todo-board-*.html (the full board
history), so a number spent on a since-rolled-off done card is never reused.

A cross-board collision detector + ratchet baseline let board_lint fail on NEW
reuse while tolerating the legacy backlog that predates the fix.

API:
  global_max_id(dir=".") -> int
  next_id(dir=".") -> str                       "tNNNN" = global_max+1
  card_titles(dir=".") -> dict[id,set]
  find_collisions(dir=".") -> dict[id,list]      ids whose title diverges across boards
  load_baseline(path) -> dict
  new_collisions(dir=".", baseline_path=None) -> dict  collisions not covered by baseline

CLI:
  board_ids.py next  [dir]
  board_ids.py max   [dir]
  board_ids.py check [dir] [baseline.json]   exit 1 on (new) collisions
  board_ids.py                               self-test
"""
from __future__ import annotations
import glob, json, os, re, sys

__all__ = ["global_max_id", "next_id", "card_titles", "find_collisions", "all_ids",
           "load_baseline", "new_collisions"]
ID_RE = re.compile(r"^t(\d{4})$")


def _boards(d):
    return sorted(glob.glob(os.path.join(d, "todo-board-*.html")))


def _items(path):
    with open(path, encoding="utf-8") as f:
        h = f.read()
    m = re.search(r'<script[^>]*id="board-data"[^>]*>(.*?)</script>', h, re.S)
    if not m:
        return []
    try:
        return json.loads(m.group(1)).get("items", [])
    except Exception:
        return []


def card_titles(d="."):
    out = {}
    for p in _boards(d):
        for it in _items(p):
            i = it.get("id", "")
            if ID_RE.match(i):
                out.setdefault(i, set()).add((it.get("title") or "").strip())
    return out


def all_ids(d="."):
    """Every card id present on ANY board in d (global). The set the pre-write
    uniqueness assert checks against - board-id-under-concurrency hardening (t0473/#16)."""
    return set(card_titles(d).keys())


def global_max_id(d="."):
    mx = 0
    for i in card_titles(d):
        mx = max(mx, int(ID_RE.match(i).group(1)))
    return mx


def next_id(d="."):
    return "t%04d" % (global_max_id(d) + 1)


def find_collisions(d="."):
    out = {}
    for i, titles in card_titles(d).items():
        distinct = sorted(t for t in titles if t)
        if len(distinct) > 1:
            out[i] = distinct
    return out


def load_baseline(path):
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {}


def new_collisions(d=".", baseline_path=None):
    base = load_baseline(baseline_path)
    out = {}
    for i, titles in find_collisions(d).items():
        if not set(titles).issubset(set(base.get(i, []))):
            out[i] = titles
    return out


def self_test():
    import tempfile
    def board(rows):
        items = [{"id": i, "title": t} for i, t in rows]
        return ('<html><script type="application/json" id="board-data">'
                + json.dumps({"items": items}).replace("</", "<\\/") + "</script></html>")
    with tempfile.TemporaryDirectory() as d:
        open(os.path.join(d, "todo-board-2026-06-11.html"), "w").write(
            board([("t0284", "Opt-Out agent"), ("t0285", "Rule 20")]))
        open(os.path.join(d, "todo-board-2026-06-12.html"), "w").write(
            board([("t0285", "Rule 20"), ("t0284", "Opt-Out agent")]))
        assert global_max_id(d) == 285 and next_id(d) == "t0286"
        assert find_collisions(d) == {}
        open(os.path.join(d, "todo-board-2026-06-13.html"), "w").write(
            board([("t0285", "DIFFERENT card reusing id")]))
        col = find_collisions(d)
        assert "t0285" in col and len(col["t0285"]) == 2
        bp = os.path.join(d, "base.json")
        json.dump({"t0285": col["t0285"]}, open(bp, "w"))
        assert new_collisions(d, bp) == {}, "baselined collision should be tolerated"
        open(os.path.join(d, "todo-board-2026-06-14.html"), "w").write(
            board([("t0285", "yet ANOTHER reuse")]))
        assert "t0285" in new_collisions(d, bp), "new title beyond baseline should flag"
        print("board_ids self-test: PASS (max/next, collision detect, ratchet baseline)")


if __name__ == "__main__":
    a = sys.argv[1:]
    if not a:
        self_test(); sys.exit(0)
    cmd = a[0]; d = a[1] if len(a) > 1 else "."
    bp = a[2] if len(a) > 2 else None
    if cmd == "next":
        print(next_id(d))
    elif cmd == "max":
        print("t%04d" % global_max_id(d))
    elif cmd == "check":
        col = new_collisions(d, bp) if bp else find_collisions(d)
        if col:
            for i, titles in sorted(col.items()):
                print("COLLISION " + i + " -> " + " | ".join(titles))
            sys.exit(1)
        print("OK: no " + ("new " if bp else "") + "id collisions across " + str(len(_boards(d))) + " boards")
    else:
        print("usage: board_ids.py [next|max|check] [dir] [baseline.json]"); sys.exit(2)
