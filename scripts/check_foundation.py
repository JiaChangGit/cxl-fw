#!/usr/bin/env python3
"""Validate the small Host scaffold; this never runs Guest project tests."""
import argparse
import ast
import hashlib
from html.parser import HTMLParser
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SKILLS = {"code-review", "bug-rca", "cpp-rust-safety", "kernel-rca", "qemu-gdb-tracing"}
sys.dont_write_bytecode = True


class Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.ids, self.links, self.stack = [], [], []

    def handle_starttag(self, tag, attrs):
        data = dict(attrs)
        if "id" in data:
            self.ids.append(data["id"])
        if tag == "a":
            self.links.append(data.get("href", ""))
        if tag == "script":
            raise ValueError("The specification must remain an offline static document")
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input",
                       "link", "meta", "param", "source", "track", "wbr"}:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if not self.stack or self.stack.pop() != tag:
            raise ValueError(f"Unbalanced HTML tag: {tag}")


def required(condition, message):
    if not condition:
        raise ValueError(message)


def check_gate_rejections():
    """Exercise failed commands and forbidden overrides in an isolated directory."""
    spec = importlib.util.spec_from_file_location(
        "paef_quality", ROOT / ".paef/scripts/quality_gate.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with tempfile.TemporaryDirectory(prefix="cxl-gate-check-") as temp:
        root = Path(temp)
        config = root / ".paef/quality.json"
        config.parent.mkdir()
        entry = {"name": "required", "argv": [sys.executable, "-c", "raise SystemExit(1)"]}
        config.write_text(json.dumps({"checks": [entry]}), encoding="utf-8")
        rc, report = module.run_gate(root, "full", write_report=False)
        required(rc != 0 and report["status"] == "FAIL", "Failed commands must fail the gate")
        config.write_text('{"checks":[]}', encoding="utf-8")
        rc, _ = module.run_gate(root, "full", write_report=False)
        required(rc != 0, "Empty checks must not pass")
        entry["argv"][-1] = "raise SystemExit(0)"
        config.write_text(json.dumps({"checks": [entry]}), encoding="utf-8")
        override = {"extra_checks": [{"name": "required", "argv": ["true"]}]}
        (config.parent / "quality.local.json").write_text(json.dumps(override), encoding="utf-8")
        rc, _ = module.run_gate(root, "full", write_report=False)
        required(rc != 0, "Local configuration must not override a required check")


def check_contract():
    for name in ("AGENTS.md", "README.md", "Makefile", ".gitignore",
                 "scripts/doctor.py", "scripts/setup_host.sh", "configs/host-packages.txt"):
        required((ROOT / name).is_file(), f"Missing scaffold file: {name}")
    source = json.loads((ROOT / ".paef/upstream.json").read_text(encoding="utf-8"))
    required(source["version"] == "2.0.0", "Unexpected PAEF source version")
    for item in source["selected_files"]:
        path = (ROOT / item["destination"]).resolve()
        required(path.is_relative_to(ROOT), "Adopted file escapes project root")
        required(path.is_file(), f"Missing adopted file: {path.name}")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        required(digest == item["sha256"], f"PAEF source drift: {item['destination']}")
    required(not (ROOT / ".paef/manager-state.json").exists(),
             "Selected-file adoption must not claim a manager installation")
    skills = {p.parent.name for p in (ROOT / ".agents/skills").glob("*/SKILL.md")}
    required(skills == SKILLS, "Unexpected skill selection")
    for name in sorted(skills):
        text = (ROOT / ".agents/skills" / name / "SKILL.md").read_text(encoding="utf-8")
        required(re.search(r"^name:\s*" + re.escape(name) + r"\s*$", text, re.M),
                 f"Skill name mismatch: {name}")
        required(re.search(r"^description:\s*\S", text, re.M),
                 f"Skill description missing: {name}")
    version = json.loads((ROOT / "configs/versions.lock.json").read_text(encoding="utf-8"))
    required(version["stage"] in {"host-foundation", "m0", "m1", "m2", "m3", "m4"}, "Check the declared project stage")
    for name, expected in (("qemu", "11.1.0"), ("linux", "6.18.56")):
        required(version[name]["version"] == expected, f"Unexpected {name} pin")
        entry = version[name]
        if entry["state"] == "not_downloaded":
            required(entry["source_sha256"] is None, f"Unexpected {name} source checksum")
        else:
            required(entry["state"] == "verified"
                     and re.fullmatch(r"[0-9a-f]{64}", entry["source_sha256"] or "")
                     and entry.get("source_url", "").startswith("https://"),
                     f"Invalid verified {name} source lock")
    quality = json.loads((ROOT / ".paef/quality.json").read_text(encoding="utf-8"))
    required(quality["profile"] in {"host-foundation", "cxl-m0", "cxl-m1", "cxl-m2", "cxl-m3", "cxl-m4"}, "Use the active project profile")
    required(bool(quality["checks"]) and "CHANGE_ME" not in json.dumps(quality),
             "Missing checks or placeholder quality configuration")
    page = Page()
    page.feed((ROOT / "cxl_memory_tiering_codex_implementation_v2.html").read_text(encoding="utf-8"))
    required(not page.stack and len(page.ids) == len(set(page.ids)), "Invalid HTML structure/IDs")
    required(all(not h.startswith("#") or h[1:] in page.ids for h in page.links),
             "Broken specification anchor")
    for folder in (ROOT / "scripts", ROOT / ".paef/scripts"):
        for path in folder.glob("*.py"):
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for folder in (ROOT / "scripts", ROOT / "guest"):
        for path in folder.glob("*.sh"):
            subprocess.run(["bash", "-n", str(path)], cwd=ROOT, check=True, timeout=15)
            subprocess.run(["shellcheck", str(path)], cwd=ROOT, check=True, timeout=15)
    check_gate_rejections()
    print("PASS: foundation contract, PAEF provenance, HTML and gate failure controls")


def check_toolchain():
    """An ephemeral Host compiler/library/sanitizer probe, not a tierlab test."""
    source = """#include <numa.h>
#include <numaif.h>
#include <optional>
#include <iostream>
int main() {
    std::optional<int> value{17};
    if (numa_available() < 0 || value.value() != 17) return 1;
    std::cout << "HOST_TOOLCHAIN_SMOKE_OK\\n";
    return 0;
}
"""
    with tempfile.TemporaryDirectory(prefix="cxl-host-smoke-") as temp:
        root = Path(temp)
        src, binary = root / "smoke.cpp", root / "smoke"
        src.write_text(source, encoding="utf-8")
        subprocess.run(["g++", "-std=c++17", "-Wall", "-Wextra", "-Werror",
                        "-fsanitize=address,undefined", "-fno-omit-frame-pointer",
                        str(src), "-lnuma", "-o", str(binary)], check=True, timeout=60)
        subprocess.run([str(binary)], check=True, timeout=15)
    print("PASS: temporary Host C++17/libnuma/ASan/UBSan smoke; Guest tests NOT_RUN")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--toolchain", action="store_true")
    args = parser.parse_args()
    try:
        check_toolchain() if args.toolchain else check_contract()
    except (ValueError, KeyError, OSError, subprocess.SubprocessError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
