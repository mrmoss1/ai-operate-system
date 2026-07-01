#!/usr/bin/env python3
"""
atomic_write.py — Canonical write path for any non-trivial file in Operate.

ZONE 1 — SHAREABLE SYSTEM. This script ships with Operate. Contains zero
tenant facts.

Why this module exists
----------------------
Cowork sessions have repeatedly truncated files mid-character when
naive Edit/Write tools are used on files larger than a few kilobytes.
Same shape every time: the tool returns success, but the file ends up
truncated mid-character somewhere in the middle of the new content.
Eight occurrences observed across Sessions 5, Session 2 of 2026-04-30
(six in one session), and Session of 2026-05-01 (one — Utah footer cut
during a CSS patch). Pattern is consistent enough that it is past
noise: the Cowork Previewer polls files on a short interval; when an
Edit lands a large diff, a partial-flush window can let the Previewer
read torn bytes that the OS then commits as the file's final state.
Or the Edit tool returns success while bytes are still in flight to
the OS. Or the Cowork mount layer has asynchronous flush semantics.
Root cause is not yet pinned; the workaround works.

This module is the workaround formalized. Every Cowork-side write — any file, any size, no exception — routes
through `atomic_write_file(...)` instead of using Edit / Write
directly. (An earlier version of this line said "larger than 5 KB";
that was a drift from canonical Rule 15.5, which has no size floor.) The function:

  * writes to a sibling temp file in the same directory (so os.replace
    is a same-filesystem atomic rename, not a cross-filesystem copy);
  * fsyncs the temp file before close;
  * uses os.replace for the rename (atomic on POSIX and Windows);
  * optionally runs a caller-provided validator on the post-rename
    bytes, raising loudly on failure rather than logging a warning.

The validator pattern lets callers assert structural invariants:
"this file ends with </html>", "this JSON parses", "this Python file
compiles", "this file is exactly N bytes". Validators run after the
rename, on the bytes that actually landed on disk — not on the
in-memory string we tried to write.

Usage
-----
    from atomic_write import atomic_write_file, ValidationError

    # Simple bytes write
    atomic_write_file(path, content_bytes)

    # With validator
    def must_end_with_html(b):
        if not b.rstrip().endswith(b"</html>"):
            raise ValidationError(f"file does not end with </html>")
    atomic_write_file(path, content_bytes, validate=must_end_with_html)

    # JSON validator (built-in convenience)
    atomic_write_file(path, json_bytes, validate=validate_json)

CLI usage
---------
The companion CLI `atomic_write_cli.py` lets bash callers route writes
through the same path:

    python "0. Chief of Staff/Agents/cos-platform/03-process/atomic_write_cli.py" /path/to/file < content

This is Rule 15.5 plumbing. See chief-of-staff.template.html > Rule 15.5.

Read side (added 2026-06-04)
----------------------------
The truncation surface includes READS. Observed 2026-06-04: a FUSE-mount
read of CLAUDE.md returned a copy truncated mid-sentence (~715 tail bytes
missing) while the host-side view of the same file was intact; the
subsequent atomic write made the truncated read durable. The write-side
validator caught it — but only because the caller happened to set a size
floor. `safe_read_file(...)` is the read-side guard: it cross-checks the
read length against os.stat (before and after the read), retries on
mismatch, and runs the same structural validators as the write side.
Use it as the read half of every read-modify-write cycle:

    src = safe_read_file(path, validate=validate_ends_with(b"</html>"))
    ... mutate in memory ...
    atomic_write_file(path, new_bytes, validate=validate_ends_with(b"</html>"))

Origin
------
Surfaced 2026-05-01 after the eighth observed Cowork-session
file-truncation event (Utah golden footer cut during a CSS patch). The
prior seven occurrences accumulated across Sessions 5 (three) and the
two sessions of 2026-04-30 (six in one session). The robustness pattern
that emerged across those repairs (find clean prefix → reconstruct
tail → atomic write → post-write parse-validate) is canonicalised here
so future agents do not have to rediscover it under fire.
"""

from __future__ import annotations

import json
import os
import datetime as _dt
import sys
from pathlib import Path
from typing import Callable, Optional, Union


__all__ = [
    "atomic_write_file",
    "safe_read_file",
    "ValidationError",
    "validate_json",
    "validate_ends_with",
    "validate_min_size",
    "validate_python_runtime_imports",
    "validate_zip",
    "validate_pdf",
    "validator_for_path",
    "self_test",
    "replace_between_anchors",
    "AnchorError",
    "begin_atomic_parts",
    "append_part",
    "finalize_atomic_parts",
]


class ValidationError(Exception):
    """Raised when a post-write validator rejects the bytes that landed."""


class AnchorError(Exception):
    """Raised by replace_between_anchors when the anchors do not resolve to
    exactly one safe, changed region (start absent/ambiguous, end missing,
    span too large, or a no-op replace)."""


def atomic_write_file(
    path: Union[str, Path],
    content: bytes,
    validate: Optional[Callable[[bytes], None]] = None,
) -> None:
    """Atomically write `content` to `path`, optionally validating after.

    Pattern: temp file in same directory + fsync + os.replace + read-back +
    optional validator. The temp file is in the same directory so the
    rename is a same-filesystem atomic operation, not a cross-fs copy.

    The validator (if given) receives the bytes read back from disk from
    the fsynced TEMP file, BEFORE the rename (contract change 2026-06-10,
    t0222). If it raises, the destination is untouched (prior good file
    preserved) and the rejected bytes are quarantined as
    `<name>.rejected-<timestamp>` beside it. After a successful rename, a
    cheap stat-size check guards the rename layer. Caveat: validators that
    dereference the destination path at validation time (rare; the runtime-
    imports validator execs the passed bytes, not the file) observe the OLD
    file during validation.

    Args:
        path: Destination file path. Parent directory must exist.
        content: Bytes to write. Pass `s.encode("utf-8")` for strings.
        validate: Optional callable taking the post-rename bytes. Must
            raise (preferably ValidationError) on failure.

    Raises:
        TypeError: content is not bytes.
        FileNotFoundError: parent directory does not exist.
        OSError: write or rename failed.
        ValidationError: validator rejected the post-rename bytes.
    """
    if not isinstance(content, (bytes, bytearray)):
        raise TypeError(
            f"atomic_write_file requires bytes, got {type(content).__name__}. "
            "Encode strings with .encode('utf-8') before calling."
        )

    path = Path(path)
    parent = path.parent
    if not parent.is_dir():
        raise FileNotFoundError(f"parent directory does not exist: {parent}")

    # Temp file in same directory so os.replace is same-filesystem atomic
    tmp = parent / f".{path.name}.atomic_write.tmp"

    try:
        # Write + fsync
        with open(tmp, "wb") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())

        # CONTRACT CHANGE 2026-06-10 (t0222, operator-approved): validate the
        # fsynced temp's landed bytes BEFORE the rename. os.replace is
        # metadata-only (content rides the same inode), so reading back the
        # temp has identical truncation-detection power to reading back the
        # dest — but a failed validation now leaves the PRIOR GOOD FILE
        # intact at dest. Worked example: the 2026-06-10 atomic_write.py
        # self-corruption incident, where post-replace validation caught the
        # break only after the dest was already clobbered.
        if validate is not None:
            with open(tmp, "rb") as f:
                landed = f.read()
            try:
                validate(landed)
            except Exception:
                # Preserve the rejected bytes for forensics. unlink() is
                # blocked on the FUSE mount without the delete grant, but
                # rename is permitted — so quarantine via rename.
                try:
                    ts = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
                    os.replace(tmp, parent / f"{path.name}.rejected-{ts}")
                except OSError:
                    pass
                raise

        # Atomic rename (POSIX guarantee; Windows os.replace also atomic)
        os.replace(tmp, path)
    except Exception:
        # Best-effort cleanup of orphaned temp
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        raise

    # Belt-and-suspenders: cheap post-replace size check (no re-validation)
    # guards the rename/mount layer itself.
    if os.stat(path).st_size != len(content):
        raise ValidationError(
            f"post-replace size mismatch at {path}: "
            f"{os.stat(path).st_size} on disk vs {len(content)} written"
        )


def safe_read_file(
    path: Union[str, Path],
    validate: Optional[Callable[[bytes], None]] = None,
    min_size: Optional[int] = None,
    retries: int = 4,                 # was 2 — the truncation is intermittent, so retries can land a full read
    retry_delay: float = 0.25,
    infer_validator: bool = True,     # NEW (variant 4)
    _stat=os.stat,
) -> bytes:
    """Guarded read for read-modify-write cycles (Rule 15.5, read side).

    Checks, per attempt: (1) os.stat size immediately before AND after the
    read must both equal the byte count actually read — a divergence means
    a torn/short read or a concurrent writer, so retry; (2) optional
    `min_size` floor; (3) optional structural `validate` callable (the same
    validators used on the write side). A validator failure is retried once
    (the file may have been mid-replace) and then raised — at that point the
    file on disk is genuinely invalid, which the caller must repair before
    writing anything back.

    Origin: 2026-06-04 CLAUDE.md read-side truncation (mount view diverged
    from host view; truncated read nearly became durable via the
    read-modify-write that followed).

    Variant 4 (2026-06-23): the sandbox mount can serve a truncated read whose
    os.stat size *also* reports the short length, so the size-consistency check
    passes on a truncated file. A structural validator is the only catch, so one
    is now inferred by extension and applied by default (override with `validate=`,
    disable with `infer_validator=False`). When reads keep failing the structural
    check, recover via the **file-tool (Read/Edit) vantage**, which reads a
    different path and sees the full file — never write back from the short read.

    Args:
        path: File to read.
        validate: Optional callable on the bytes read; raise to reject.
        min_size: Optional minimum byte count.
        retries: Re-attempts after a failed check (total attempts = retries+1).
        retry_delay: Seconds to sleep between attempts.
        infer_validator: When True and no `validate` is supplied, infer a
            structural validator from the file extension (variant 4).
        _stat: Test seam; leave default.

    Returns:
        The file's bytes, size-consistent and validator-approved.

    Raises:
        ValidationError: checks still failing after all retries.
        OSError / FileNotFoundError: underlying read failures.
    """
    import time as _time

    path = Path(path)
    # Rule 15.5(d) variant 4: size-consistency misses a vantage-consistent
    # truncation; apply a structural validator inferred from the extension
    # unless the caller supplied one or opted out.
    if validate is None and infer_validator:
        validate = validator_for_path(path)
    last_err: Optional[str] = None
    for attempt in range(retries + 1):
        if attempt:
            _time.sleep(retry_delay)
        size_before = _stat(path).st_size
        with open(path, "rb") as f:
            data = f.read()
        size_after = _stat(path).st_size
        if not (len(data) == size_before == size_after):
            last_err = (
                f"short/torn read: stat-before={size_before}, "
                f"read={len(data)}, stat-after={size_after}"
            )
            continue
        if min_size is not None and len(data) < min_size:
            last_err = f"file smaller than floor: {len(data)} < {min_size}"
            continue
        if validate is not None:
            try:
                validate(data)
            except Exception as e:
                last_err = f"read validator rejected: {e}"
                continue
        return data
    raise ValidationError(
        f"safe_read_file: {path} failed after {retries + 1} attempts — {last_err}"
    )


def replace_between_anchors(
    text: str,
    start: str,
    end: str,
    new: str,
    *,
    max_span: Optional[int] = None,
) -> str:
    """Replace the span from `start` to `end` (both inclusive) with `new`.

    The vetted recipe for in-place section edits to structured text — a
    CLAUDE.md section, a dossier ``<li>``, a board ``<script>`` block. It
    enforces the invariants a hand-rolled ``text.replace()`` or manual slice
    silently skips — the omission that wasted a write attempt on 2026-06-09:

      * `start` occurs EXACTLY ONCE in `text` (0 -> not found; >1 -> ambiguous,
        refuse to guess which region was meant);
      * `end` is the FIRST occurrence at/after the end of `start`, so `end` may
        be a common token like ``</li>`` and still resolve correctly;
      * the matched span length is <= `max_span` when given — the guard that
        stops a common `end` anchor from swallowing half the file;
      * the result actually DIFFERS from the input — catches a no-op where
        `new` equals the region it replaced.

    Compose `new` to include whatever anchors you want to keep; this function
    replaces the WHOLE span, anchors included.

    IMPORTANT: anchor on plain, verbatim tokens you typed exactly. Never anchor
    on a formatted fragment carrying markdown emphasis (``*``, ``_``) or HTML
    tags reconstructed from memory — asserting on ``replaces the prior`` while
    the source held ``*replaces* the prior`` is precisely the false-negative
    this helper exists to retire.

    Pure function: returns the new full text and never touches disk. Pass the
    result to ``atomic_write_file(...)`` with an appropriate validator.

    Raises:
        AnchorError: any invariant fails.
    """
    n_start = text.count(start)
    if n_start != 1:
        raise AnchorError(
            f"start anchor must occur exactly once, found {n_start}: {start!r}"
        )
    i = text.index(start)
    after_start = i + len(start)
    j = text.find(end, after_start)
    if j == -1:
        raise AnchorError(f"end anchor not found at/after start: {end!r}")
    end_pos = j + len(end)
    span_len = end_pos - i
    if max_span is not None and span_len > max_span:
        raise AnchorError(
            f"matched span {span_len} exceeds max_span {max_span} — the end "
            f"anchor {end!r} likely matched too far downstream; tighten the "
            f"anchors or raise max_span deliberately"
        )
    old_span = text[i:end_pos]
    if new == old_span:
        raise AnchorError("no-op replace: `new` is identical to the matched span")
    return text[:i] + new + text[end_pos:]


# ─────────────────────────────────────────────────────────────────────────
# Chunked "parts" assembly — for files too large for a single heredoc command
# ─────────────────────────────────────────────────────────────────────────
#
# The command-length ceiling: a very long `python3 << 'PYEOF' ... PYEOF`
# invocation can be truncated by the calling tool BEFORE bash sees the closing
# delimiter — it fails with "unexpected EOF while looking for matching quote"
# and the content never reaches Python (observed 2026-06-23). The fix is NOT to
# chain N atomic_write_file calls to the destination (that N-multiplies the
# destination truncation surface). Instead assemble the file in a sibling temp
# across multiple short commands, then do exactly ONE atomic, validated replace:
#
#     begin_atomic_parts(dest)                  # once: create/truncate the temp
#     append_part(dest, chunk_a)                # one short command per chunk
#     append_part(dest, chunk_b)
#     finalize_atomic_parts(dest, validate=...) # validate temp, single os.replace
#
# The destination still sees a single atomic, validated write; only the DELIVERY
# of the bytes is chunked.


def _parts_tmp(path: Path) -> Path:
    return path.parent / f".{path.name}.atomic_parts.tmp"


def begin_atomic_parts(path: Union[str, Path]) -> Path:
    """Start a chunked write: create (or truncate) the sibling parts temp.

    Call once before the first append_part. Opening "wb" truncates, so a fresh
    begin discards any abandoned prior assembly. Returns the temp path.
    """
    path = Path(path)
    parent = path.parent
    if not parent.is_dir():
        raise FileNotFoundError(f"parent directory does not exist: {parent}")
    tmp = _parts_tmp(path)
    with open(tmp, "wb") as f:
        f.flush()
        os.fsync(f.fileno())
    return tmp


def append_part(path: Union[str, Path], chunk: bytes) -> None:
    """Append one chunk of bytes to the parts temp opened by begin_atomic_parts.

    Each call is its own (short) command, so an arbitrarily large file can be
    delivered across many invocations without any single command approaching the
    tool's command-length ceiling. Append + flush + fsync. Raises if begin was
    not called first.
    """
    if not isinstance(chunk, (bytes, bytearray)):
        raise TypeError(
            f"append_part requires bytes, got {type(chunk).__name__}. "
            "Encode strings with .encode('utf-8') before calling."
        )
    path = Path(path)
    tmp = _parts_tmp(path)
    if not tmp.exists():
        raise FileNotFoundError(
            f"parts temp missing — call begin_atomic_parts({path.name!r}) first: {tmp}"
        )
    with open(tmp, "ab") as f:
        f.write(chunk)
        f.flush()
        os.fsync(f.fileno())


def finalize_atomic_parts(
    path: Union[str, Path],
    validate: Optional[Callable[[bytes], None]] = None,
) -> int:
    """Finish a chunked write: validate the assembled temp, then atomically
    replace the destination with it in a single os.replace.

    Mirrors atomic_write_file's validate-before-replace contract: the validator
    runs on the assembled temp's landed bytes BEFORE the rename, so a failure
    leaves the prior good file intact at the destination and quarantines the
    assembled bytes as `<name>.rejected-<ts>` beside it. Returns the byte count
    written. Raises if begin was not called.
    """
    path = Path(path)
    tmp = _parts_tmp(path)
    if not tmp.exists():
        raise FileNotFoundError(
            f"parts temp missing — nothing to finalize for {path.name!r}: {tmp}"
        )
    with open(tmp, "rb") as f:
        landed = f.read()
    if validate is not None:
        try:
            validate(landed)
        except Exception:
            try:
                ts = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
                os.replace(tmp, path.parent / f"{path.name}.rejected-{ts}")
            except OSError:
                pass
            raise
    os.replace(tmp, path)
    if os.stat(path).st_size != len(landed):
        raise ValidationError(
            f"post-replace size mismatch at {path}: "
            f"{os.stat(path).st_size} on disk vs {len(landed)} assembled"
        )
    return len(landed)



# ─────────────────────────────────────────────────────────────────────────
# Built-in validators
# ─────────────────────────────────────────────────────────────────────────


def validate_json(content: bytes) -> None:
    """Raise ValidationError if content does not parse as JSON."""
    try:
        json.loads(content.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise ValidationError(f"post-write JSON validation failed: {e}") from e


def validate_ends_with(suffix: bytes) -> Callable[[bytes], None]:
    """Return a validator that requires the file to end with `suffix`.

    Trailing whitespace is stripped before comparison. Use for HTML
    (suffix=b"</html>"), XML, etc.
    """
    def _check(content: bytes) -> None:
        if not content.rstrip().endswith(suffix):
            tail = content[-min(80, len(content)):]
            raise ValidationError(
                f"post-write file does not end with {suffix!r}. "
                f"Last 80 bytes: {tail!r}"
            )
    return _check


def validate_min_size(min_bytes: int) -> Callable[[bytes], None]:
    """Return a validator that requires the file to be at least `min_bytes`.

    Use as a sanity check against severe truncation when you know the
    content should be at least N bytes.
    """
    def _check(content: bytes) -> None:
        if len(content) < min_bytes:
            raise ValidationError(
                f"post-write file too small: got {len(content)} bytes, "
                f"expected at least {min_bytes}"
            )
    return _check


def validator_for_path(path):
    """Infer a structural read-validator from the file extension (Rule 15.5(d), read side).

    Size-consistency alone (len == stat-before == stat-after) does NOT catch a
    *vantage-consistent* truncation: the sandbox FUSE mount can present a short
    read AND a matching short os.stat size, so the consistency check passes on a
    truncated file. A structural validator is the only catch. Returns a
    callable(bytes)->None, or None when no structural check is known.
    """
    s = str(path).lower()
    if s.endswith((".html", ".htm")):
        return validate_ends_with(b"</html>")
    if s.endswith(".svg"):
        return validate_ends_with(b"</svg>")
    if s.endswith(".json"):
        return validate_json
    if s.endswith(".py"):
        def _py(content):
            import ast
            ast.parse(content.decode("utf-8"))
        return _py
    return None


def validate_zip(content: bytes) -> None:
    """Validator: content must be a complete, CRC-clean ZIP archive.

    Parses the bytes with zipfile and runs testzip() (CRC check on every
    member). Use for any archive written via atomic_write_file — build the
    archive in memory (io.BytesIO), then atomic-write the bytes with this
    validator. Added 2026-06-10 (hardening t0222): replaces hand-rolled
    temp+os.replace zip writes that lacked fsync + helper discipline.
    """
    import io
    import zipfile
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            bad = zf.testzip()
    except zipfile.BadZipFile as exc:
        raise ValidationError(f"post-write validation failed: not a valid ZIP: {exc}")
    if bad is not None:
        raise ValidationError(f"post-write validation failed: CRC error on member {bad!r}")


def validate_pdf(content: bytes) -> None:
    """Validator: content must be a structurally-complete PDF.

    Checks the ``%PDF-`` header and a ``%%EOF`` trailer — the structural
    bookends a truncated PDF loses. Dependency-free: the cheap structural
    assertion, the binary analogue of ``validate_ends_with(b"</html>")``. Use
    for any PDF written via atomic_write_file: render into an in-memory
    ``io.BytesIO``, then atomic-write the bytes with this validator. Added
    2026-06-23 (t0393 binary-artifact sweep).
    """
    if not content.startswith(b"%PDF-"):
        raise ValidationError(
            "post-write validation failed: not a PDF (missing %PDF- header); "
            f"first 8 bytes: {content[:8]!r}"
        )
    if b"%%EOF" not in content[-1024:]:
        raise ValidationError(
            "post-write validation failed: PDF has no %%EOF trailer in the last "
            "1024 bytes — likely truncated"
        )


def validate_python_runtime_imports(
    module_path: Optional[Union[str, Path]] = None,
    extra_syspath: Optional[list] = None,
) -> Callable[[bytes], None]:
    """Return a validator that executes a Python file's MODULE BODY to catch
    import-time runtime errors that ``py_compile`` misses.

    Why this exists
    ---------------
    ``py_compile`` (and ``compile(src, ..., "exec")``) only check *syntax* —
    they never run module-level code. A file can compile cleanly yet raise the
    moment it is imported. The canonical case (surfaced 2026-05-24): a
    module-level f-string whose ``{...}`` references a name that is not defined
    compiles fine but raises ``NameError`` at import. This validator runs the
    module body so that class of bug fails the write instead of shipping.

    How it works
    ------------
    Compiles the bytes, then ``exec``s the resulting code object in an isolated
    namespace with:
      * ``__name__`` set to a synthetic value (NOT ``"__main__"``) — so a
        ``if __name__ == "__main__":`` entry-point block does NOT run. The check
        verifies *import*, never the script's main logic.
      * ``__file__`` set to ``module_path`` — so module-level ``Path(__file__)``
        resolves. Pass the destination path (recommended). If omitted, ``__file__``
        is a placeholder and modules that dereference it at import will fail.
    The module's own directory (and any ``extra_syspath`` entries) are placed on
    ``sys.path`` for the duration so sibling imports resolve, then removed.

    Caveat
    ------
    This EXECUTES module-level code. It is intended for well-behaved modules
    whose import is side-effect-free (imports + definitions + a ``__main__``
    guard) — the Operate convention. Do NOT use it on a module that does real
    work (network, file writes, long compute) at import time.

    Args:
        module_path: Destination path of the file being validated. Used for
            ``__file__`` and to add the file's directory to ``sys.path``.
        extra_syspath: Optional list of additional directories to put on
            ``sys.path`` during the check (e.g. a helper library location).

    Returns:
        A validator callable suitable for ``atomic_write_file(..., validate=...)``.
    """
    def _check(content: bytes) -> None:
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as e:
            raise ValidationError(
                f"runtime-import check: file is not valid UTF-8: {e}"
            ) from e

        fname = str(module_path) if module_path is not None else "<atomic_write_runtime_check>"
        try:
            code = compile(text, fname, "exec")
        except SyntaxError as e:
            raise ValidationError(f"runtime-import check: SyntaxError: {e}") from e

        ns = {
            "__name__": "__atomic_write_runtime_check__",  # NOT __main__: skip entry point
            "__file__": fname,
            "__builtins__": __builtins__,
        }

        added: list = []
        candidates = list(extra_syspath or [])
        if module_path is not None:
            candidates.insert(0, str(Path(module_path).resolve().parent))
        for d in candidates:
            if d and d not in sys.path:
                sys.path.insert(0, d)
                added.append(d)

        try:
            exec(code, ns)
        except (Exception, SystemExit) as e:
            raise ValidationError(
                f"runtime-import check: module body raised "
                f"{type(e).__name__}: {e}"
            ) from e
        finally:
            for d in added:
                try:
                    sys.path.remove(d)
                except ValueError:
                    pass
    return _check


# ─────────────────────────────────────────────────────────────────────────
# Self-test
# ─────────────────────────────────────────────────────────────────────────


def self_test() -> None:
    """Run an end-to-end self-test. Raises on any failure."""
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)

        # Test 1: simple write round-trips
        p1 = td_path / "test1.txt"
        atomic_write_file(p1, b"hello world\n")
        assert p1.read_bytes() == b"hello world\n", "test1: round-trip mismatch"

        # Test 2: validator passes
        p2 = td_path / "test2.html"
        atomic_write_file(
            p2,
            b"<html><body>ok</body></html>\n",
            validate=validate_ends_with(b"</html>"),
        )
        assert p2.read_bytes().rstrip().endswith(b"</html>")

        # Test 3: validator rejects
        p3 = td_path / "test3.html"
        try:
            atomic_write_file(
                p3,
                b"<html><body>truncated",
                validate=validate_ends_with(b"</html>"),
            )
        except ValidationError:
            pass  # expected
        else:
            raise AssertionError("test3: validator should have rejected")

        # Test 4: JSON validator
        p4 = td_path / "test4.json"
        atomic_write_file(
            p4,
            b'{"key": "value"}',
            validate=validate_json,
        )

        # Test 5: JSON validator rejects torn JSON
        p5 = td_path / "test5.json"
        try:
            atomic_write_file(
                p5,
                b'{"key": "val',
                validate=validate_json,
            )
        except ValidationError:
            pass  # expected
        else:
            raise AssertionError("test5: JSON validator should have rejected")

        # Test 6: bytes-only contract enforced
        p6 = td_path / "test6.txt"
        try:
            atomic_write_file(p6, "string not bytes")  # type: ignore
        except TypeError:
            pass  # expected
        else:
            raise AssertionError("test6: should have rejected str")

        # Test 7: missing parent directory rejected
        p7 = td_path / "nope" / "test7.txt"
        try:
            atomic_write_file(p7, b"x")
        except FileNotFoundError:
            pass  # expected
        else:
            raise AssertionError("test7: should have rejected missing parent")

        # Test 8: temp file cleaned up after success
        p8 = td_path / "test8.txt"
        atomic_write_file(p8, b"clean")
        leftover = list(td_path.glob(".test8.txt.atomic_write.tmp"))
        assert not leftover, f"test8: temp file leaked: {leftover}"

        # Test 9: min-size validator
        p9 = td_path / "test9.txt"
        try:
            atomic_write_file(p9, b"x", validate=validate_min_size(100))
        except ValidationError:
            pass  # expected
        else:
            raise AssertionError("test9: min-size validator should have rejected")

        # Test 10: runtime-import validator PASSES on a clean module
        #          (well-formed f-string + a __main__ guard that must NOT run).
        p10 = td_path / "test10.py"
        good = (
            "import os\n"
            "GREETING = f\"hello {os.name}\"\n"
            "def main():\n"
            "    raise RuntimeError('main should not run during validation')\n"
            "if __name__ == '__main__':\n"
            "    main()\n"
        )
        atomic_write_file(
            p10, good.encode("utf-8"),
            validate=validate_python_runtime_imports(p10),
        )

        # Test 11: runtime-import validator REJECTS a module that compiles but
        #          crashes at import — module-level f-string referencing an
        #          undefined name (the 2026-05-24 failure class py_compile misses).
        p11 = td_path / "test11.py"
        bad = "BANNER = f\"value is {undefined_name_here}\"\n"
        compile(bad, "test11.py", "exec")  # proves it is syntactically valid
        try:
            atomic_write_file(
                p11, bad.encode("utf-8"),
                validate=validate_python_runtime_imports(p11),
            )
        except ValidationError:
            pass  # expected — NameError at import is caught
        else:
            raise AssertionError("test11: runtime-import validator should have rejected")

        # Test 12: the __main__ guard is honored — a module whose main() would
        #          raise still PASSES, proving we import without running main.
        p12 = td_path / "test12.py"
        guarded = (
            "def main():\n"
            "    raise SystemExit('main ran — should not happen')\n"
            "if __name__ == '__main__':\n"
            "    main()\n"
        )
        atomic_write_file(
            p12, guarded.encode("utf-8"),
            validate=validate_python_runtime_imports(p12),
        )

        # Test 13: safe_read_file round-trips with validator
        p13 = td_path / "test13.html"
        atomic_write_file(p13, b"<html><body>ok</body></html>\n")
        got = safe_read_file(p13, validate=validate_ends_with(b"</html>"))
        assert got.rstrip().endswith(b"</html>"), "test13: bad read"

        # Test 14: safe_read_file raises on genuinely-truncated on-disk file
        p14 = td_path / "test14.html"
        atomic_write_file(p14, b"<html><body>trunca")
        try:
            safe_read_file(p14, validate=validate_ends_with(b"</html>"),
                           retries=1, retry_delay=0.01)
        except ValidationError:
            pass  # expected
        else:
            raise AssertionError("test14: should have rejected truncated file")

        # Test 15: stat-mismatch path — a lying stat (simulating the
        #          2026-06-04 mount-view divergence) trips the size
        #          cross-check; a persistent lie exhausts retries.
        p15 = td_path / "test15.txt"
        atomic_write_file(p15, b"0123456789")
        class _LyingStat:
            def __init__(self, real): self.real = real
            def __call__(self, p):
                s = self.real(p)
                class R: st_size = s.st_size + 5
                return R()
        try:
            safe_read_file(p15, retries=1, retry_delay=0.01,
                           _stat=_LyingStat(os.stat))
        except ValidationError as e:
            assert "short/torn read" in str(e), f"test15: wrong error: {e}"
        else:
            raise AssertionError("test15: lying stat should have raised")
        assert safe_read_file(p15) == b"0123456789", "test15: honest read failed"
        # ── replace_between_anchors (pure-string section-replace recipe) ──
        # Test 16: happy path — unique anchors, span replaced, result changes
        _src = "HEAD\n## Sec\nold body\n(end-marker)\nTAIL\n"
        _out = replace_between_anchors(
            _src, "## Sec", "(end-marker)", "## Sec\nNEW body\n(end-marker)")
        assert _out == "HEAD\n## Sec\nNEW body\n(end-marker)\nTAIL\n", f"test16: {_out!r}"

        # Test 17: start anchor absent -> AnchorError
        try:
            replace_between_anchors("abc", "## Missing", "x", "y")
        except AnchorError:
            pass
        else:
            raise AssertionError("test17: missing start should raise")

        # Test 18: start anchor ambiguous (twice) -> AnchorError
        try:
            replace_between_anchors("## S ... ## S", "## S", "end", "z")
        except AnchorError:
            pass
        else:
            raise AssertionError("test18: ambiguous start should raise")

        # Test 19: span exceeds max_span (runaway end anchor) -> AnchorError
        _runaway = "START" + ("." * 50) + "</li>" + ("." * 50) + "</li>"
        try:
            replace_between_anchors(_runaway, "START", "</li>", "X", max_span=10)
        except AnchorError:
            pass
        else:
            raise AssertionError("test19: oversize span should raise")

        # Test 20: no-op replace (new == old span) -> AnchorError
        try:
            replace_between_anchors("aSTARTbEND c", "START", "END", "STARTbEND")
        except AnchorError:
            pass
        else:
            raise AssertionError("test20: no-op replace should raise")

        # Test 21: non-unique end is fine — first at/after start is used (the
        # real <li>...</li> case), guarded by max_span
        _multi = "<li>A</li>\n<li>TARGET stuff</li>\n<li>C</li>\n"
        _out21 = replace_between_anchors(
            _multi, "<li>TARGET", "</li>", "<li>TARGET replaced</li>", max_span=40)
        assert _out21 == "<li>A</li>\n<li>TARGET replaced</li>\n<li>C</li>\n", f"test21: {_out21!r}"


        # ── chunked parts assembly (begin/append/finalize) ──
        # Test 22: parts assembly round-trips identically to a one-shot write
        p22 = td_path / "test22.txt"
        begin_atomic_parts(p22)
        append_part(p22, b"alpha\n")
        append_part(p22, b"beta\n")
        append_part(p22, b"gamma\n")
        n22 = finalize_atomic_parts(p22)
        assert p22.read_bytes() == b"alpha\nbeta\ngamma\n", "test22: assembled mismatch"
        assert n22 == 17, "test22: byte count wrong"

        # Test 23: finalize validator rejects -> prior good file preserved,
        #          rejected bytes quarantined, destination unchanged
        p23 = td_path / "test23.html"
        atomic_write_file(p23, b"<html>prior</html>\n")
        begin_atomic_parts(p23)
        append_part(p23, b"<html>new but ")
        append_part(p23, b"truncated")
        try:
            finalize_atomic_parts(p23, validate=validate_ends_with(b"</html>"))
        except ValidationError:
            pass  # expected
        else:
            raise AssertionError("test23: finalize validator should have rejected")
        assert p23.read_bytes() == b"<html>prior</html>\n", "test23: prior file clobbered"
        assert any(x.name.startswith("test23.html.rejected-") for x in td_path.iterdir()), "test23: rejected bytes not quarantined"

        # Test 24: append before begin raises (no temp to append to)
        p24 = td_path / "test24.txt"
        try:
            append_part(p24, b"x")
        except FileNotFoundError:
            pass  # expected
        else:
            raise AssertionError("test24: append before begin should raise")

        # Test 25 (variant 4): inferred structural validator rejects a
        # structurally-incomplete read even when the file is size-consistent
        # on disk (simulating a vantage-consistent truncation).
        p25 = td_path / "trunc.html"
        with open(p25, "w", encoding="utf-8") as f:
            f.write("<html><body>no closing tag")   # complete on disk but no </html>
        try:
            safe_read_file(p25)                       # infer_validator picks ends-with </html>
            raise AssertionError("test25: expected ValidationError on missing </html>")
        except ValidationError:
            pass
        # opt-out path returns the bytes unchecked
        assert safe_read_file(p25, infer_validator=False).endswith(b"no closing tag")

        # Test 26: validate_pdf accepts header+trailer, rejects truncated / non-PDF
        atomic_write_file(td_path / "test26.pdf",
                          b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n%%EOF\n",
                          validate=validate_pdf)
        for bad, why in ((b"%PDF-1.4\n(truncated, no eof", "missing %%EOF"),
                         (b"not a pdf at all", "missing %PDF- header")):
            try:
                atomic_write_file(td_path / "test26bad.pdf", bad, validate=validate_pdf)
            except ValidationError:
                pass
            else:
                raise AssertionError("test26: validate_pdf should reject (" + why + ")")

    print("atomic_write self-test: 26/26 passed")




def _self_test_zip() -> None:
    """Self-test for validate_zip (called from the __main__ block)."""
    import io
    import tempfile
    import zipfile
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "t.zip"
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("a.txt", "hello")
        atomic_write_file(p, buf.getvalue(), validate=validate_zip)
        try:
            atomic_write_file(p, b"not a zip", validate=validate_zip)
        except ValidationError:
            pass
        else:
            raise AssertionError("validate_zip accepted garbage")
        # CONTRACT (2026-06-10, validate-before-replace): a failed write
        # leaves the PRIOR GOOD FILE intact at dest and quarantines the
        # rejected bytes beside it.
        with zipfile.ZipFile(p) as z:
            assert z.read("a.txt") == b"hello", "prior good file was not preserved"
        assert any(x.name.startswith("t.zip.rejected-") for x in p.parent.iterdir()), "rejected bytes not quarantined"


if __name__ == "__main__":
    self_test()
    _self_test_zip()
