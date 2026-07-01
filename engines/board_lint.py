#!/usr/bin/env python3
"""board_lint.py - Rule corollary: validate a pickup-brief to-do board.

ZONE 1 - SHAREABLE SYSTEM. Zero tenant facts. The executable contract for
the board card schema + portfolio linkage. The add-todo FORM enforces these
invariants in JavaScript; programmatic/agent writes had no equivalent check,
which let ~4 cards drift chip-less (t0195 et al., 2026-06-09). This linter is
that check, runnable from any write path and at session-close.

This is a Python corollary to the board-card discipline in CLAUDE.md
(work-logging hook) and the Board Steward dossier: a card is valid only if it
passes board_lint. Prose points here instead of re-specifying the schema.

Checks (ERROR unless marked WARN):
  * id is "t" followed by exactly 4 digits (zero-padded)
  * board-data.items[] and initial-state{} cover the same id set
  * theme is an int matching a declared portfolio theme num (1..6 fallback)
  * epic_id is non-empty and (when portfolio-data is present) exists in epics
  * epic_title is non-empty
  * okrs is a list (WARN if empty)
  * verdict, if present, is one of dotoday/schedule/delegate/defer/done
    (absent verdict = backlog, allowed)
  * all three JSON blocks parse (board-data, initial-state, portfolio-data)
  * embed-safe: no embedded block contains a literal </script

CLI:  python3 board_lint.py <board.html> [more.html ...]   # exit 1 on any ERROR
      python3 board_lint.py                                  # run self-test
"""
from __future__ import annotations

# >>> t0560 data-root (walk-up: CoS root via shim or real home) <<<
import os as _os0560
_t0560 = _os0560.path.abspath(__file__)
for _ in range(10):
    _t0560 = _os0560.path.dirname(_t0560)
    if _os0560.path.basename(_t0560) == "0. Chief of Staff" or _os0560.path.isfile(_os0560.path.join(_t0560, "tenant.json")):
        break
_T0560_COS = _t0560
_T0560_OP = _os0560.path.dirname(_T0560_COS)
# <<< t0560 >>>

import json
import re
import sys
from pathlib import Path

__all__ = ["lint_board", "lint_files", "self_test", "VALID_VERDICTS",
           "closure_reason", "CLOSURE_MARKERS"]

VALID_VERDICTS = {"dotoday", "schedule", "delegate", "defer", "done"}
ID_RE = re.compile(r"^t\d{4}$")

# --- S10: closure taxonomy (sister-spec Decision S10) -----------------------
# A card is CLOSED iff verdict == 'done'. Among closed cards a LEADING notes
# marker selects the closure REASON; absent any marker => 'delivered'. 'descoped'
# rides as done + a DESCOPED marker (distinct from WON'T DO) so it buckets as
# validated learning, never as REALIZED output -- no new verdict in the schema.
CLOSURE_MARKERS = {
    "killed":         ("WON'T DO", "WONT DO"),
    "descoped-pivot": ("DESCOPED",),
    "superseded":     ("SUPERSEDED",),
}


def closure_reason(state_entry):
    """Terminal closure reason of a card per S10, or None if not closed.

    done + "WON'T DO ..."   -> 'killed'
    done + "DESCOPED ..."   -> 'descoped-pivot'
    done + "SUPERSEDED ..." -> 'superseded'
    done (no leading marker)-> 'delivered'   (the only reason that is REALIZED output)
    verdict != 'done'       -> None          (open / backlog, not a closure)
    """
    if not isinstance(state_entry, dict):
        return None
    if state_entry.get("verdict") != "done":
        return None
    notes = (state_entry.get("notes") or "").lstrip().upper()
    for reason, prefixes in CLOSURE_MARKERS.items():
        if any(notes.startswith(p) for p in prefixes):
            return reason
    return "delivered"


def _raw_block(html, sid):
    m = re.search(r'<script[^>]*id="' + sid + r'"[^>]*>(.*?)</script>', html, re.S)
    return m.group(1).strip() if m else None


_MILE_CACHE = None
def _milestone_ids():
    """Known milestone ids from milestones.json (t0242). Cached; empty set if missing."""
    global _MILE_CACHE
    if _MILE_CACHE is None:
        try:
            import os as _os
            p = _os.path.join(_T0560_COS, "milestones.json")
            with open(p, encoding="utf-8") as f:
                _MILE_CACHE = {m.get("id") for m in json.load(f).get("milestones", []) if m.get("id")}
        except Exception:
            _MILE_CACHE = set()
    return _MILE_CACHE

def lint_board(path):
    """Return (errors, warnings): two lists of strings for one board file."""
    errors, warnings = [], []
    html = Path(path).read_text(encoding="utf-8")
    blocks = {}
    for sid in ("board-data", "initial-state", "portfolio-data"):
        raw = _raw_block(html, sid)
        if raw is None:
            errors.append("missing JSON block: " + sid)
            continue
        if "</script" in raw.lower():
            errors.append(sid + ": embedded block contains literal </script (embed-safe escape missing)")
        try:
            blocks[sid] = json.loads(raw)
        except Exception as e:
            errors.append(sid + " does not parse: " + str(e))
    if "board-data" not in blocks or "initial-state" not in blocks:
        return errors, warnings

    items = blocks["board-data"].get("items", [])
    state = blocks["initial-state"]
    iset = {it.get("id") for it in items}
    sset = set(state)
    for tid in sorted(x for x in iset - sset if x):
        errors.append(str(tid) + ": in board-data but missing initial-state entry")
    for tid in sorted(sset - iset):
        errors.append(str(tid) + ": in initial-state but missing board-data item")

    epic_ids = set()
    theme_nums = set()
    pd = blocks.get("portfolio-data")
    if isinstance(pd, dict):
        epic_ids = {e.get("id") for e in pd.get("epics", []) if isinstance(e, dict)}
        theme_nums = {t.get("num") for t in pd.get("themes", []) if isinstance(t, dict) and isinstance(t.get("num"), int)}

    for tid in sorted(iset & sset):
        if not (isinstance(tid, str) and ID_RE.match(tid)):
            errors.append(str(tid) + ": id is not 't' + 4 digits")
        st = state[tid]
        if not isinstance(st, dict):
            errors.append(str(tid) + ": state entry is not an object")
            continue
        v = st.get("verdict")
        if v is not None and v not in VALID_VERDICTS:
            errors.append(tid + ": invalid verdict " + repr(v))
        # S10: a LEADING closure marker (WON'T DO / DESCOPED / SUPERSEDED) is a
        # terminal disposition and must sit on a 'done' card, never an open one.
        _nlead = (st.get("notes") or "").lstrip().upper()
        for _reason, _prefixes in CLOSURE_MARKERS.items():
            if any(_nlead.startswith(_p) for _p in _prefixes):
                if v != "done":
                    errors.append(tid + ": leading " + _reason
                                  + " closure marker requires verdict 'done', got " + repr(v))
                break
        theme = st.get("theme")
        _theme_ok = isinstance(theme, int) and (theme in theme_nums if theme_nums else 1 <= theme <= 6)
        if not _theme_ok:
            errors.append(tid + ": theme must be a declared portfolio theme num" + (" " + repr(sorted(theme_nums)) if theme_nums else " (1-6)") + ", got " + repr(theme))
        epic = st.get("epic_id")
        if not (isinstance(epic, str) and epic):
            errors.append(tid + ": epic_id missing/empty")
        elif epic_ids and epic not in epic_ids:
            errors.append(tid + ": epic_id " + repr(epic) + " not in portfolio epics")
        if not (isinstance(st.get("epic_title"), str) and st.get("epic_title")):
            errors.append(tid + ": epic_title missing/empty")
        okrs = st.get("okrs")
        if not isinstance(okrs, list):
            errors.append(tid + ": okrs must be a list, got " + type(okrs).__name__)
        elif not okrs:
            warnings.append(tid + ": okrs empty")
        # soft milestone field (t0242): absent is valid (legacy cards pass); unknown id WARNs
        mile = st.get("milestone")
        if mile is not None:
            if not (isinstance(mile, str) and mile):
                warnings.append(tid + ": milestone must be a non-empty string id (or absent)")
            elif _milestone_ids() and mile not in _milestone_ids():
                warnings.append(tid + ": milestone " + repr(mile) + " not in milestones.json")
    return errors, warnings


def lint_files(paths):
    """Lint each path; print findings; return total ERROR count."""
    total = 0
    for p in paths:
        errs, warns = lint_board(p)
        name = Path(p).name
        for w in warns:
            print("WARN  " + name + ": " + w)
        for e in errs:
            print("ERROR " + name + ": " + e)
        if not errs:
            print("OK    " + name + ": clean (" + str(len(warns)) + " warn)")
        total += len(errs)
    return total


def _mk_board(state, items, epics=(("E1", 1),)):
    pd = {"themes": [{"num": 1}], "epics": [{"id": e, "theme": t} for e, t in epics]}
    bd = {"board_date": "2026-06-09", "items": items, "state_version": 1}
    def emb(o):
        return json.dumps(o).replace("</", "<\\/")
    return ("<html><body>"
            '<script type="application/json" id="board-data">' + emb(bd) + "</script>"
            '<script type="application/json" id="initial-state">' + emb(state) + "</script>"
            '<script type="application/json" id="portfolio-data">' + emb(pd) + "</script>"
            "</body></html>")


def self_test():
    import tempfile
    good_state = {"t0001": {"verdict": "done", "theme": 1, "epic_id": "E1",
                            "epic_title": "Epic One", "okrs": ["kr"]}}
    good_items = [{"id": "t0001", "title": "x", "desc": "", "ev": "", "src": "", "init": None}]

    with tempfile.TemporaryDirectory() as td:
        def write(html):
            p = Path(td) / "b.html"
            p.write_text(html, encoding="utf-8")
            return str(p)

        # 1: valid board -> no errors
        e, w = lint_board(write(_mk_board(good_state, good_items)))
        assert e == [], "test1 valid board should be clean: " + str(e)

        # 2: missing theme -> error
        s = {"t0001": {"verdict": "done", "epic_id": "E1", "epic_title": "Epic One", "okrs": ["kr"]}}
        e, w = lint_board(write(_mk_board(s, good_items)))
        assert any("theme" in x for x in e), "test2 missing theme should error"

        # 3: bad id (not 4-digit) -> error
        s = {"t12": {"verdict": "done", "theme": 1, "epic_id": "E1", "epic_title": "E", "okrs": ["k"]}}
        it = [{"id": "t12", "title": "x"}]
        e, w = lint_board(write(_mk_board(s, it)))
        assert any("4 digits" in x for x in e), "test3 bad id should error"

        # 4: epic not in portfolio -> error
        s = {"t0001": {"verdict": "done", "theme": 1, "epic_id": "E9", "epic_title": "E", "okrs": ["k"]}}
        e, w = lint_board(write(_mk_board(s, good_items)))
        assert any("not in portfolio" in x for x in e), "test4 unknown epic should error"

        # 5: empty okrs -> WARN not error
        s = {"t0001": {"verdict": "done", "theme": 1, "epic_id": "E1", "epic_title": "E", "okrs": []}}
        e, w = lint_board(write(_mk_board(s, good_items)))
        assert e == [] and any("okrs empty" in x for x in w), "test5 empty okrs should warn only"

        # 6: id coverage mismatch -> error
        e, w = lint_board(write(_mk_board(good_state, [{"id": "t0002", "title": "x"}])))
        assert any("missing" in x for x in e), "test6 coverage mismatch should error"

        # 7: backlog (no verdict) with portfolio fields -> clean
        s = {"t0001": {"theme": 1, "epic_id": "E1", "epic_title": "E", "okrs": ["k"]}}
        e, w = lint_board(write(_mk_board(s, good_items)))
        assert e == [], "test7 backlog card should be clean: " + str(e)

        # 8: bad verdict -> error
        s = {"t0001": {"verdict": "wip", "theme": 1, "epic_id": "E1", "epic_title": "E", "okrs": ["k"]}}
        e, w = lint_board(write(_mk_board(s, good_items)))
        assert any("invalid verdict" in x for x in e), "test8 bad verdict should error"

        # 9: closure_reason classifier
        assert closure_reason({"verdict": "done", "notes": "DESCOPED - pivot E307 2026-06-24"}) == "descoped-pivot"
        assert closure_reason({"verdict": "done", "notes": "WON'T DO 2026-06-24: de-scoped"}) == "killed"
        assert closure_reason({"verdict": "done", "notes": "SUPERSEDED by t0399"}) == "superseded"
        assert closure_reason({"verdict": "done", "notes": "shipped"}) == "delivered"
        assert closure_reason({"verdict": "dotoday", "notes": "DESCOPED later"}) is None

        # 10: DESCOPED marker on a done card -> clean
        s = {"t0001": {"verdict": "done", "theme": 1, "epic_id": "E1", "epic_title": "E",
                       "okrs": ["k"], "notes": "DESCOPED - pivot E1 2026-06-24"}}
        e, w = lint_board(write(_mk_board(s, good_items)))
        assert e == [], "test10 descoped-on-done should be clean: " + str(e)

        # 11: DESCOPED marker on a NON-done card -> error
        s = {"t0001": {"verdict": "dotoday", "theme": 1, "epic_id": "E1", "epic_title": "E",
                       "okrs": ["k"], "notes": "DESCOPED - pivot"}}
        e, w = lint_board(write(_mk_board(s, good_items)))
        assert any("closure marker requires verdict 'done'" in x for x in e), "test11 marker-on-open should error"

    print("board_lint self-test: 11/11 passed")


def lint_id_consistency(paths):
    """Cross-board ratchet: ERROR on id reuse NOT covered by the legacy baseline.
    Delegates to board_ids.new_collisions; no-op if board_ids or the baseline
    file is unavailable. The baseline (.board-id-collisions-baseline.json) freezes
    the pre-fix legacy collisions so only NEW reuse fails the gate (t0288 fix)."""
    import os
    errs = []
    try:
        import board_ids
    except Exception as e:
        print("WARN cross-board id check skipped (board_ids import): " + str(e))
        return errs
    seen = set()
    for p in paths:
        d = os.path.dirname(os.path.abspath(p)) or "."
        if d in seen:
            continue
        seen.add(d)
        base = os.path.join(d, ".board-id-collisions-baseline.json")
        if not os.path.exists(base):
            continue
        for i, titles in sorted(board_ids.new_collisions(d, base).items()):
            errs.append("cross-board: id " + i + " reused for distinct cards: " + " | ".join(titles))
    return errs


if __name__ == "__main__":
    argv = sys.argv[1:]
    if not argv:
        self_test()
    else:
        total = lint_files(argv)
        for e in lint_id_consistency(argv):
            print("ERROR " + e)
            total += 1
        sys.exit(1 if total else 0)
