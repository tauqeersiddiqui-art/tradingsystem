# -*- coding: utf-8 -*-
"""Fix UTF-8 double-encoded mojibake in source files.

The source files were saved with UTF-8 bytes re-interpreted as latin-1 and
re-encoded as UTF-8, so em-dashes, emojis, arrows etc. show as garbled
Ã...Â... sequences. Reverse the corruption: latin-1-encode -> utf-8-decode,
repeatedly until stable. Idempotent and safe for already-clean files.
"""
import io
import sys

TARGETS = [
    "master_runner.py",
    "engine/live_engine.py",
    "engine/config/config.py",
    "ml/ml_intraday_learner.py",
]


def unmojibake(s: str) -> str:
    for _ in range(6):
        try:
            dec = s.encode("latin-1", errors="strict").decode("utf-8", errors="strict")
        except (UnicodeEncodeError, UnicodeDecodeError):
            break
        if dec == s:
            break
        s = dec
    return s


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    any_bad = False
    for rel in TARGETS:
        p = rel
        try:
            raw = io.open(p, "r", encoding="utf-8", errors="strict").read()
        except FileNotFoundError:
            print(f"[SKIP] {rel} not found")
            continue
        except UnicodeDecodeError as e:
            print(f"[SKIP] {rel} invalid utf-8: {e}")
            continue
        if "�" in raw:
            print(f"[ABORT] {rel} contains U+FFFD replacement chars; aborting")
            any_bad = True
            continue
        fixed = unmojibake(raw)
        n_before = raw.count("Ã")
        n_after = fixed.count("Ã")
        if n_before == 0 and raw == fixed:
            print(f"[CLEAN] {rel} no mojibake found")
            continue
        io.open(p, "w", encoding="utf-8", newline="").write(fixed)
        print(f"[FIXED] {rel} markers {n_before} -> {n_after} | bytes {len(raw)} -> {len(fixed)}")
    if any_bad:
        sys.exit(2)


if __name__ == "__main__":
    main()
