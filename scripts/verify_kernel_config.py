#!/usr/bin/env python3
"""Verify requested fragment values against the final pinned kernel config."""
import argparse
import json
from pathlib import Path
import re


def read_config(path):
    values = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        enabled = re.fullmatch(r"(CONFIG_[A-Z0-9_]+)=(.*)", line)
        disabled = re.fullmatch(r"# (CONFIG_[A-Z0-9_]+) is not set", line)
        if enabled:
            values[enabled[1]] = enabled[2]
        elif disabled:
            values[disabled[1]] = "n"
    return values


def verify(config, fragment, source=None):
    desired = read_config(fragment)
    if not desired:
        raise ValueError("Kernel fragment has no settings")
    actual = read_config(config)
    symbols = None
    if source is not None:
        symbols = set()
        for path in Path(source).rglob("Kconfig*"):
            if path.is_file():
                symbols.update(re.findall(r"^\s*(?:menuconfig|config)\s+([A-Z0-9_]+)\s*$",
                                          path.read_text(encoding="utf-8", errors="replace"), re.M))
    errors = []
    for key, value in desired.items():
        if symbols is not None and key.removeprefix("CONFIG_") not in symbols:
            errors.append(f"Unknown symbol: {key}")
        elif actual.get(key, "n") != value:
            errors.append(f"{key}: expected {value}, got {actual.get(key, 'n')}")
    if errors:
        raise ValueError("; ".join(errors))
    return {"status": "PASS", "checked_symbols": len(desired)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path)
    parser.add_argument("--fragment", type=Path, default=Path("configs/kernel.fragment"))
    parser.add_argument("--source", type=Path)
    args = parser.parse_args()
    try:
        print(json.dumps(verify(args.config, args.fragment, args.source)))
        return 0
    except (ValueError, OSError) as exc:
        print(f"FAIL: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
