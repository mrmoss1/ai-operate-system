#!/usr/bin/env python3
"""board_add.py - canonical board-card constructor (the EMIT half of the board pair).

ZONE 1 - SHAREABLE SYSTEM. board_lint VALIDATES card shape; board_ids RATCHETS the
global id; board_backfill PROPOSES portfolio linkage - but nothing EMITS a
well-formed card. Agents therefore reverse-engineer the card shape from existing
cards (observed 2026-06-23: a card was reconstructed by dumping an existing card
from BOTH the board-data and initial-state blocks just to learn the shape). This
module closes that constructor/validator asymmetry.

Two layers:
  make_card(...) -> {"tid", "item", "state"}   PURE: builds the matched
        (board-data item, initial-state entry) pair, resolves portfolio linkage
        from the target board's portfolio-data, pulls a fresh GLOBAL id via
        board_ids.next_id, and SELF-VALIDATES via board_lint before returning.
  add_card(board_path, ...) -> tid             ORCHESTRATION: read board, insert
        the pair into both JSON blocks, atomic-write (Rule 15.5) embed-safe, and
        re-lint the landed file. REFUSES a truncated read (source must end with
        </html>) - the guard motivated by the 2026-06-23 shell-mount truncation.

This removes the hand-rolled minified-JSON path and its truncation / embed-safety
risk: the only sanctioned way to mint a card is make_card / add_card.

CLI:
  python3 board_add.py <board.html> --title "..." --verdict schedule --epic E305 \
          [--theme 3] [--notes "..."] [--desc "..."] [--ev "path"] \
          [--src "..."] [--okr "..."]... [--milestone M] [--seeded] [--dry-run]
  python3 board_add.py                       # self-test
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sys
import tempfile
from pathlib import Path

import board_ids
import board_lint
from atomic_write import atomic_write_file

__all__ = ["make_card", "add_card", "self_test", "BoardAddError"]

VALID_VERDICTS = board_lint.VALID_VERDICTS  # single source of truth
ID_RE = re.compile(r"^t\d{4}$")


class BoardAddError(ValueError):
    """Raised on any card-construction or board-write violation."""


# ----------------------------------------------------------------------------- helpers
def _block(html, sid):
    m = re.search(r'<script[^>]*id="' + sid + r'"[^>]*>(.*?)</script>', html, re.S)
    return json.loads(m.group(1).strip()) if m else None


def _emb(obj):
    """Serialize compact + embed-safe: no literal </script can survive in the payload."""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")


def _utcnow():
    return datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def _resolve_epic(portfolio, epic_id):
    epics = (portfolio or {}).get("epics", []) if isinstance(portfolio, dict) else []
    return next((e for e in epics if isinstance(e, dict) and e.get("id") == epic_id), None)


# ----------------------------------------------------------------------------- core
def make_card(title, verdict, *, theme=None, epic_id=None, epic_title=None,
              okrs=None, desc=None, src=None, ev=None, notes=None,
              decided_at=None, seeded=False, milestone=None, tid=None,
              portfolio=None, board_dir="."):
    """Build a matched (board-data item, initial-state entry) pair for one card.

    When `epic_id` is given and `epic_title` / `theme` / `okrs` are omitted, they
    are resolved from `portfolio` (the target board's parsed portfolio-data). When
    `tid` is None it is pulled via board_ids.next_id(board_dir) - the GLOBAL max
    across all boards, never the per-board nextTId (which reuses ids after a
    rollover drops done cards). The pair is SELF-VALIDATED via board_lint before
    return; any violation raises BoardAddError.

    Returns: {"tid": str, "item": dict, "state": dict}
    """
    if not title or not str(title).strip():
        raise BoardAddError("title is required and non-empty")
    if verdict not in VALID_VERDICTS:
        raise BoardAddError(f"verdict {verdict!r} not in {sorted(VALID_VERDICTS)}")

    if tid is None:
        tid = board_ids.next_id(board_dir)
    if not (isinstance(tid, str) and ID_RE.match(tid)):
        raise BoardAddError(f"tid {tid!r} is not 't' + 4 digits")

    epic_obj = _resolve_epic(portfolio, epic_id) if epic_id else None
    if epic_id and epic_obj is None and portfolio is not None:
        raise BoardAddError(f"epic_id {epic_id!r} not found in portfolio epics")
    if epic_title is None:
        epic_title = epic_obj.get("title") if epic_obj else None
    if theme is None:
        theme = epic_obj.get("theme") if epic_obj else None
    if okrs is None:
        okrs = list((epic_obj.get("okrs") or [])[:1]) if epic_obj else []

    # schema floor (mirrors board_lint; raised here for clearer, earlier errors)
    # Range is 1-6: board_lint's fallback is 1..6 and the live portfolio declares Theme 6
    # (Career Enablement). board_lint remains the real gate (validates against declared
    # portfolio theme nums on the actual board). Swept 2026-06-25 (Rule 20): was 1-5 and
    # rejected valid Theme-6 cards despite 16 already live on the board.
    if not (isinstance(theme, int) and 1 <= theme <= 6):
        raise BoardAddError(
            f"theme must be int 1-6, got {theme!r} - pass theme= or an epic that carries a theme")
    if not (isinstance(epic_id, str) and epic_id):
        raise BoardAddError("epic_id missing/empty")
    if not (isinstance(epic_title, str) and epic_title):
        raise BoardAddError("epic_title missing/empty and not resolvable from portfolio")
    if not isinstance(okrs, list):
        raise BoardAddError("okrs must be a list")

    title = str(title).strip()
    item = {
        "id": tid,
        "src": src or "",
        "title": title,
        "desc": desc if desc is not None else f"[{verdict.upper()}] {title}",
        "ev": ev,
        "init": None,
    }
    state = {
        "verdict": verdict,
        "decidedAt": decided_at or _utcnow(),
        "seeded": bool(seeded),
        "notes": notes or "",
        "theme": theme,
        "epic_id": epic_id,
        "epic_title": epic_title,
        "okrs": okrs,
    }
    if milestone:
        state["milestone"] = milestone

    _self_validate(tid, item, state, portfolio)
    return {"tid": tid, "item": item, "state": state}


def _self_validate(tid, item, state, portfolio):
    """Run board_lint over a synthesized minimal board containing only this card."""
    epics = [{"id": e.get("id"), "theme": e.get("theme")}
             for e in (portfolio or {}).get("epics", []) if isinstance(e, dict)]
    if not epics:  # no portfolio supplied: trust the card's own linkage
        epics = [{"id": state["epic_id"], "theme": state["theme"]}]
    # Mirror the REAL portfolio's declared themes (incl. Theme 6 Career Enablement);
    # do not hardcode 1-5. Fallback 1-6 matches board_lint. Swept 2026-06-25 (Rule 20):
    # range(1,6) discarded Theme 6 and rejected valid T6 cards in self-validation.
    declared = [th for th in (portfolio or {}).get("themes", [])
                if isinstance(th, dict) and isinstance(th.get("num"), int)]
    themes = declared if declared else [{"num": n} for n in range(1, 7)]
    pd = {"themes": themes, "epics": epics}
    bd = {"board_date": "9999-01-01", "items": [item], "state_version": 1}
    html = ("<html><body>"
            '<script type="application/json" id="board-data">' + _emb(bd) + "</script>"
            '<script type="application/json" id="initial-state">' + _emb({tid: state}) + "</script>"
            '<script type="application/json" id="portfolio-data">' + _emb(pd) + "</script>"
            "</body></html>")
    errors, _ = _lint_html(html)
    if errors:
        raise BoardAddError("self-validation failed: " + "; ".join(errors))


def _lint_html(html):
    with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False, encoding="utf-8") as f:
        f.write(html)
        tmp = f.name
    try:
        return board_lint.lint_board(tmp)
    finally:
        os.unlink(tmp)


def _replace_block(html, sid, obj):
    pat = re.compile(r'(<script[^>]*id="' + sid + r'"[^>]*>)(.*?)(</script>)', re.S)
    if not pat.search(html):
        raise BoardAddError("block not found: " + sid)
    return pat.sub(lambda m: m.group(1) + _emb(obj) + m.group(3), html, count=1)


# ----------------------------------------------------------------------------- orchestration
def add_card(board_path, title, verdict, *, dry_run=False, **kw):
    """Append a make_card() pair to both JSON blocks of `board_path`, atomic-write, re-lint.

    REFUSES a truncated read (source must end with </html>) - per the
    Write-Propagation Guard, a partial shell-mount read must never be written back.
    Returns the new tid (dry_run=True validates + returns the tid without writing).
    """
    board_path = str(board_path)
    board_dir = os.path.dirname(board_path) or "."
    html = Path(board_path).read_text(encoding="utf-8")
    if not html.rstrip().endswith("</html>"):
        raise BoardAddError(
            "refusing to write: source board does not end with </html> (truncated/partial "
            "read - Write-Propagation Guard). Re-read in a clean sandbox before writing.")

    portfolio = _block(html, "portfolio-data")
    kw.setdefault("portfolio", portfolio)
    kw.setdefault("board_dir", board_dir)
    card = make_card(title, verdict, **kw)
    tid, item, state = card["tid"], card["item"], card["state"]

    bd = _block(html, "board-data") or {"items": []}
    isd = _block(html, "initial-state") or {}
    if tid in {it.get("id") for it in bd.get("items", [])} or tid in isd:
        raise BoardAddError(f"id collision: {tid} already on the board")

    # Pre-write GLOBAL uniqueness (board-id-under-concurrency hardening, t0473 / #16):
    # reject a tid that collides on ANY board in board_dir, not just the target board.
    # Re-scanned from disk here so a concurrent writer that claimed this id between our
    # read and write (or a stale next_id from two simultaneous sessions) is caught, not
    # silently duplicated. The target board is included in the scan; a freshly-minted tid
    # (global_max+1) is absent everywhere and passes.
    global_ids = board_ids.all_ids(board_dir)
    if tid in global_ids:
        raise BoardAddError(
            f"id collision (global): {tid} already exists on another board in {board_dir!r} "
            "- concurrent writer or stale next_id; re-mint via board_ids.next_id()")

    bd.setdefault("items", []).append(item)
    if isinstance(bd.get("next_id_floor"), int):
        bd["next_id_floor"] = max(bd["next_id_floor"], int(tid[1:]) + 1)
    isd[tid] = state

    new_html = _replace_block(html, "board-data", bd)
    new_html = _replace_block(new_html, "initial-state", isd)
    if not new_html.rstrip().endswith("</html>"):
        raise BoardAddError("assembled board lost its </html> tail - aborting (no write)")

    if dry_run:
        errors, _ = _lint_html(new_html)
        if errors:
            raise BoardAddError("dry-run board_lint errors: " + "; ".join(errors))
        return tid

    def _validate(b):
        h = b.decode("utf-8")
        if not h.rstrip().endswith("</html>"):
            raise ValueError("validator: missing </html>")
        errs, _ = _lint_html(h)
        if errs:
            raise ValueError("validator: board_lint: " + "; ".join(errs))
        if tid not in (_block(h, "initial-state") or {}):
            raise ValueError("validator: tid missing from initial-state")

    atomic_write_file(board_path, new_html.encode("utf-8"), validate=_validate)

    errors, _ = board_lint.lint_board(board_path)
    if errors:
        raise BoardAddError("post-write board_lint errors: " + "; ".join(errors))
    return tid


# ----------------------------------------------------------------------------- self-test
def self_test():
    import shutil

    def mk_board(d, date="2099-01-01", items=None, state=None):
        epics = [{"id": "E305", "theme": 3, "title": "Rule 15.5 + Atomic Write Infrastructure",
                  "okrs": ["KR3.6 zero file-truncation incidents"]},
                 {"id": "E201", "theme": 2, "title": "Marketing Stack", "okrs": ["KR2.1 ship"]}]
        pd = {"themes": [{"num": n} for n in range(1, 6)], "epics": epics}
        bd = {"board_date": date, "items": items or [], "state_version": 1, "next_id_floor": 1}
        html = ("<html><body>"
                '<script type="application/json" id="board-data">' + _emb(bd) + "</script>"
                '<script type="application/json" id="initial-state">' + _emb(state or {}) + "</script>"
                '<script type="application/json" id="portfolio-data">' + _emb(pd) + "</script>"
                "</body></html>")
        p = os.path.join(d, "todo-board-%s.html" % date)
        Path(p).write_text(html, encoding="utf-8")
        return p, pd

    d = tempfile.mkdtemp()
    try:
        board, pd = mk_board(d)

        # 1. make_card resolves epic_title/theme/okrs from portfolio, mints global id
        c = make_card("Build a thing", "schedule", epic_id="E305",
                      portfolio=pd, board_dir=d, notes="why")
        assert c["tid"] == "t0001", c["tid"]
        assert c["state"]["theme"] == 3 and c["state"]["epic_title"].startswith("Rule 15.5")
        assert c["state"]["okrs"] == ["KR3.6 zero file-truncation incidents"]
        assert c["item"]["id"] == "t0001" and c["item"]["init"] is None
        assert c["item"]["desc"].startswith("[SCHEDULE]")

        # 2. matched pair: board-data item id == initial-state key
        assert c["item"]["id"] == c["tid"]

        # 3. bad verdict / bad epic / missing theme all raise
        for bad in (lambda: make_card("x", "nope", epic_id="E305", portfolio=pd),
                    lambda: make_card("x", "schedule", epic_id="E999", portfolio=pd),
                    lambda: make_card("", "schedule", epic_id="E305", portfolio=pd),
                    lambda: make_card("x", "schedule", portfolio={"epics": []})):
            try:
                bad(); raise AssertionError("expected BoardAddError")
            except BoardAddError:
                pass

        # 4. add_card end-to-end: lands a card, file stays lint-clean and whole
        tid = add_card(board, "First card", "schedule", epic_id="E305", notes="n1")
        assert tid == "t0001", tid
        errs, _ = board_lint.lint_board(board)
        assert not errs, errs
        h = Path(board).read_text(encoding="utf-8")
        assert h.rstrip().endswith("</html>")
        assert tid in (_block(h, "initial-state"))
        assert any(it["id"] == tid for it in _block(h, "board-data")["items"])

        # 5. id ratchets across the now-2-card board; second add gets t0002
        tid2 = add_card(board, "Second card", "delegate", epic_id="E201",
                        notes="n2", milestone=None)
        assert tid2 == "t0002", tid2
        assert not board_lint.lint_board(board)[0]

        # 6. id-collision guard
        try:
            add_card(board, "dup", "schedule", epic_id="E305", tid="t0001")
            raise AssertionError("expected collision BoardAddError")
        except BoardAddError:
            pass

        # 7. truncated-read guard: a source without </html> is refused
        trunc = os.path.join(d, "todo-board-2099-02-02.html")
        Path(trunc).write_text(Path(board).read_text(encoding="utf-8")[:-50], encoding="utf-8")
        try:
            add_card(trunc, "x", "schedule", epic_id="E305")
            raise AssertionError("expected truncated-read BoardAddError")
        except BoardAddError as e:
            assert "truncated" in str(e).lower() or "</html>" in str(e)

        # 8. dry_run validates without writing
        before = Path(board).read_text(encoding="utf-8")
        tid3 = add_card(board, "Third", "defer", epic_id="E305", dry_run=True)
        assert tid3 == "t0003"
        assert Path(board).read_text(encoding="utf-8") == before  # unchanged

        print("board_add self-test: PASS (make_card resolve+validate, add_card "
              "round-trip, id ratchet, collision + truncated-read guards, dry-run)")
        return 0
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ----------------------------------------------------------------------------- CLI
def _main(argv):
    if not argv:
        return self_test()
    ap = argparse.ArgumentParser(description="Add one card to a to-do board (canonical constructor).")
    ap.add_argument("board")
    ap.add_argument("--title", required=True)
    ap.add_argument("--verdict", required=True, choices=sorted(VALID_VERDICTS))
    ap.add_argument("--epic", dest="epic_id")
    ap.add_argument("--theme", type=int)
    ap.add_argument("--title-desc", dest="desc")
    ap.add_argument("--desc", dest="desc")
    ap.add_argument("--ev")
    ap.add_argument("--src")
    ap.add_argument("--notes")
    ap.add_argument("--okr", action="append", dest="okrs")
    ap.add_argument("--milestone")
    ap.add_argument("--seeded", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    tid = add_card(a.board, a.title, a.verdict, epic_id=a.epic_id, theme=a.theme,
                   desc=a.desc, ev=a.ev, src=a.src, notes=a.notes, okrs=a.okrs,
                   milestone=a.milestone, seeded=a.seeded, dry_run=a.dry_run)
    print(("DRY-RUN ok, would add " if a.dry_run else "added ") + tid + " to " + a.board)
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
