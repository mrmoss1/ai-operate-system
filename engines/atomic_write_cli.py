#!/usr/bin/env python3
"""
atomic_write_cli.py — bash-callable wrapper around atomic_write_file().

ZONE 1 — SHAREABLE SYSTEM. No tenant facts.

Reads bytes from stdin, writes them to the given path via the canonical
atomic_write_file() helper. Optionally runs a built-in validator.

Usage (one-shot write):
    cat newfile.html | python "0. Chief of Staff/Agents/cos-platform/03-process/atomic_write_cli.py" /path/to/dest.html
    cat newfile.html | python "0. Chief of Staff/Agents/cos-platform/03-process/atomic_write_cli.py" --validate=ends-with-html /path/to/dest.html
    cat config.json   | python "0. Chief of Staff/Agents/cos-platform/03-process/atomic_write_cli.py" --validate=json /path/to/config.json
    cat script.py     | python "0. Chief of Staff/Agents/cos-platform/03-process/atomic_write_cli.py" --validate=python-runtime-imports /path/to/script.py

Usage (chunked write — for a file too large for one command; see Rule 15.5,
"Files too large for a single heredoc"). Each command is short, so an
arbitrarily large file is delivered across many invocations while the
destination still sees a SINGLE atomic, validated replace:
    python "0. Chief of Staff/Agents/cos-platform/03-process/atomic_write_cli.py" --begin /path/to/dest                 # once
    cat chunk_a | python "0. Chief of Staff/Agents/cos-platform/03-process/atomic_write_cli.py" --append /path/to/dest  # per chunk
    cat chunk_b | python "0. Chief of Staff/Agents/cos-platform/03-process/atomic_write_cli.py" --append /path/to/dest
    python "0. Chief of Staff/Agents/cos-platform/03-process/atomic_write_cli.py" --finalize --validate=ends-with-html /path/to/dest

Available validators (--validate=NAME):
    json                    — content parses as JSON
    ends-with-html          — file ends with </html>
    ends-with-newline       — file ends with a newline
    zip                     — content is a complete, CRC-clean ZIP archive
    pdf                     — content is a structurally-complete PDF (%PDF- header + %%EOF trailer)
    python-runtime-imports  — Python file's MODULE BODY executes without error
                              (catches import-time crashes py_compile misses). The
                              destination path is passed so __file__ resolves and
                              sibling imports work; the __main__ guard is NOT run.
                              Use only for import-side-effect-free modules.
    none                    — no validation
    (--validate applies to a one-shot write or to --finalize.)

Exit codes:
    0  — success
    1  — write succeeded but validation failed (file/temp on disk; inspect)
    2  — write failed (no file changed)
    3  — usage error

This is Rule 15.5 plumbing. See chief-of-staff.template.html > Rule 15.5.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make the helper importable when called from anywhere
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from atomic_write import (  # noqa: E402
    atomic_write_file,
    begin_atomic_parts,
    append_part,
    finalize_atomic_parts,
    ValidationError,
    validate_json,
    validate_ends_with,
    validate_min_size,
    validate_python_runtime_imports,
    validate_zip,
    validate_pdf,
)


# Static validators (name -> instance). Path-dependent validators are resolved
# once the destination is known (they need the destination path at call time).
VALIDATORS = {
    "zip": validate_zip,
    "pdf": validate_pdf,
    "json": validate_json,
    "ends-with-html": validate_ends_with(b"</html>"),
    "ends-with-newline": validate_ends_with(b"\n"),
    "none": None,
}
PATH_DEPENDENT = {"python-runtime-imports"}
MODES = {"--begin", "--append", "--finalize"}


def usage_and_exit(msg: str = "") -> None:
    if msg:
        print(f"error: {msg}", file=sys.stderr)
    print(__doc__, file=sys.stderr)
    sys.exit(3)


def _resolve_validator(name: str, dest: str):
    if name in PATH_DEPENDENT:
        return validate_python_runtime_imports(dest)
    return VALIDATORS[name]


def main(argv: list[str]) -> int:
    args = argv[1:]
    if not args:
        usage_and_exit("missing destination path")

    mode = None
    validator_name = None
    while args and args[0].startswith("--"):
        arg = args.pop(0)
        if arg in MODES:
            if mode is not None:
                usage_and_exit(f"only one of {sorted(MODES)} allowed")
            mode = arg
        elif arg.startswith("--validate="):
            validator_name = arg.split("=", 1)[1]
            available = list(VALIDATORS) + sorted(PATH_DEPENDENT)
            if validator_name not in available:
                usage_and_exit(f"unknown validator: {validator_name}. Available: {available}")
        elif arg in ("-h", "--help"):
            usage_and_exit()
        else:
            usage_and_exit(f"unknown flag: {arg}")

    if len(args) != 1:
        usage_and_exit(f"expected exactly one path, got {len(args)}: {args}")
    dest = args[0]

    # --validate is meaningful only for a one-shot write or for --finalize.
    if validator_name is not None and mode in (None, "--finalize"):
        validator = _resolve_validator(validator_name, dest)
    elif validator_name is not None:
        usage_and_exit(f"--validate is only valid for a one-shot write or --finalize, not {mode}")
        return 3  # unreachable; keeps type-checkers happy
    else:
        validator = None

    try:
        if mode == "--begin":
            begin_atomic_parts(dest)
            print(f"OK: began chunked write to {dest}", file=sys.stderr)
            return 0
        if mode == "--append":
            chunk = sys.stdin.buffer.read()
            append_part(dest, chunk)
            print(f"OK: appended {len(chunk)} bytes to parts temp for {dest}", file=sys.stderr)
            return 0
        if mode == "--finalize":
            n = finalize_atomic_parts(dest, validate=validator)
            print(f"OK: finalized {n} bytes to {dest}", file=sys.stderr)
            return 0
        # default: one-shot stdin write
        content = sys.stdin.buffer.read()
        atomic_write_file(dest, content, validate=validator)
        print(f"OK: wrote {len(content)} bytes to {dest}", file=sys.stderr)
        return 0
    except ValidationError as e:
        print(f"VALIDATION FAILED: {e}", file=sys.stderr)
        print(f"Destination/temp for {dest} on disk; inspect before trusting.", file=sys.stderr)
        return 1
    except (OSError, TypeError) as e:
        print(f"WRITE FAILED: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
