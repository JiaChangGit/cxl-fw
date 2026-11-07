#!/usr/bin/env python3
"""Build the pinned QEMU/kernel with verified inputs and one heavy-work lock."""
import argparse
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.parse
import urllib.request

from verify_kernel_config import verify

ROOT = Path(__file__).resolve().parents[1]
PINS = {"qemu": "11.1.0", "linux": "6.18.56"}


def sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def validated_entry(lock, name):
    entry = lock[name]
    if entry["version"] != PINS[name]:
        raise ValueError(f"Unexpected {name} version; changing pins requires an explicit decision")
    digest = entry.get("source_sha256")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError(f"{name} needs a verified source_sha256 in versions.lock.json")
    url = entry.get("source_url", "")
    parsed = urllib.parse.urlparse(url)
    expected = f"{name}-{entry['version']}.tar.xz"
    if parsed.scheme != "https" or Path(parsed.path).name != expected:
        raise ValueError(f"{name} needs an HTTPS source_url ending in {expected}")
    return {**entry, "filename": expected}


def download(entry, directory):
    directory.mkdir(parents=True, exist_ok=True)
    dest = directory / entry["filename"]
    if not dest.exists():
        part = dest.with_suffix(dest.suffix + ".part")
        if part.is_symlink():
            raise ValueError(f"Refusing symlink download temporary file: {part}")
        started = time.monotonic()
        try:
            with urllib.request.urlopen(entry["source_url"], timeout=60) as response:
                if urllib.parse.urlparse(response.url).scheme != "https":
                    raise ValueError("Refusing a non-HTTPS download redirect")
                with part.open("wb") as stream:
                    while chunk := response.read(1024 * 1024):
                        stream.write(chunk)
                        if time.monotonic() - started > 1200:
                            raise TimeoutError("Source download exceeded 1200 seconds")
            if sha256(part) != entry["source_sha256"]:
                raise ValueError(f"Downloaded checksum mismatch: {dest.name}")
            part.replace(dest)
        except BaseException:
            part.unlink(missing_ok=True)
            raise
    if dest.is_symlink() or sha256(dest) != entry["source_sha256"]:
        raise ValueError(f"Cached source checksum mismatch: {dest}")
    return dest


def extract(archive, sources, top, digest):
    sources.mkdir(parents=True, exist_ok=True)
    dest = sources / top
    stamp = sources / (top + ".source.json")
    if dest.is_symlink():
        raise ValueError(f"Refusing a symlink source directory: {dest}")
    if dest.exists():
        if not stamp.is_file() or json.loads(stamp.read_text())["source_sha256"] != digest:
            raise ValueError(f"Existing source has no matching extraction provenance: {dest}")
        return dest
    # A temporary sibling makes failed extraction distinguishable from a valid source.
    with tempfile.TemporaryDirectory(prefix=".extract-", dir=sources) as temp:
        with tarfile.open(archive, "r:*") as bundle:
            members, excluded = [], []
            for member in bundle.getmembers():
                parts = Path(member.name).parts
                if not parts or parts[0] != top or ".." in parts:
                    raise ValueError(f"Archive member outside expected source root: {member.name}")
                # Upstream bundles one macOS-only EDK2 include symlink. It is not
                # used to build x86_64-softmmu; do not weaken tar's data filter.
                if (member.name == "qemu-11.1.0/roms/edk2/EmulatorPkg/Unix/Host/X11IncludeHack"
                        and member.issym() and member.linkname == "/opt/X11/include"):
                    excluded.append({"path": member.name, "link_target": member.linkname,
                                     "reason": "Unused EDK2 macOS X11 include symlink"})
                    continue
                members.append(member)
            bundle.extractall(temp, members=members, filter="data")
        (Path(temp) / top).rename(dest)
    stamp.write_text(json.dumps({"source_sha256": digest, "excluded_members": excluded}) + "\n")
    for item in excluded:
        print(f"Excluded unused archive symlink: {item['path']} -> {item['link_target']}", flush=True)
    return dest


def run(argv, cwd, log, timeout=7200):
    line = "$ " + shlex.join([str(x) for x in argv])
    print(line, flush=True)
    log.write(line + "\n")
    log.flush()
    process = subprocess.Popen([str(x) for x in argv], cwd=cwd, stdout=log,
                               stderr=subprocess.STDOUT, start_new_session=True)
    try:
        rc = process.wait(timeout=timeout)
    except BaseException:
        # Stop the compiler/ninja process group too, before releasing the heavy lock.
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        raise
    if rc:
        raise subprocess.CalledProcessError(rc, argv)


def write_metadata(path, data):
    temp = path.with_suffix(".json.tmp")
    temp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def build_qemu(entry, source, jobs, log, log_path):
    out = ROOT / "build/qemu"
    out.mkdir(parents=True, exist_ok=True)
    prefix = ROOT / "install/qemu"
    configure = [str(source / "configure"), f"--prefix={prefix}",
                 "--target-list=x86_64-softmmu", "--enable-virtfs",
                 "--disable-docs", "--disable-werror", "--disable-download"]
    stamp = out / "build-meta.json"
    prior = json.loads(stamp.read_text()) if stamp.exists() else {}
    if not (out / "build.ninja").exists() or prior.get("configure") != configure:
        run(configure, out, log, timeout=600)
    run(["ninja", "-j", str(jobs), "install"], out, log)
    binary = prefix / "bin/qemu-system-x86_64"
    version = subprocess.check_output([binary, "--version"], text=True, timeout=10).splitlines()[0]
    if "version 11.1.0" not in version:
        raise ValueError(f"Unexpected installed QEMU version: {version}")
    devices = subprocess.check_output([binary, "-device", "help"], text=True, timeout=15)
    if "cxl-type3" not in devices or "virtio-9p-pci" not in devices:
        raise ValueError("Built QEMU lacks CXL Type-3 or virtio-9p")
    data = {"status": "BUILT", "version": entry["version"],
            "source_sha256": entry["source_sha256"], "source_dir": str(source),
            "source_exclusions": json.loads(
                (source.parent / (source.name + ".source.json")).read_text()).get("excluded_members", []),
            "build_dir": str(out), "configure": configure, "binary": str(binary),
            "binary_sha256": sha256(binary), "version_output": version,
            "build_jobs": jobs, "log": str(log_path)}
    write_metadata(stamp, data)
    print(f"BUILT QEMU: {binary}", flush=True)


def build_kernel(entry, source, jobs, log, log_path):
    out = ROOT / "build/kernel"
    out.mkdir(parents=True, exist_ok=True)
    fragment = ROOT / "configs/kernel.fragment"
    config = out / ".config"
    run(["make", "-C", source, f"O={out}", "x86_64_defconfig"], ROOT, log, timeout=180)
    run(["bash", source / "scripts/kconfig/merge_config.sh", "-m", "-O", out,
         config, fragment], source, log, timeout=180)
    run(["make", "-C", source, f"O={out}", "olddefconfig"], ROOT, log, timeout=180)
    audit = verify(config, fragment, source)
    write_metadata(out / "config-audit.json", audit)
    run(["make", "-C", source, f"O={out}", f"-j{jobs}", "bzImage", "modules"], ROOT, log)
    release = subprocess.check_output(
        ["make", "-s", "-C", source, f"O={out}", "kernelrelease"], text=True, timeout=30).strip()
    if release != entry["version"]:
        raise ValueError(f"Unexpected kernelrelease: {release}")
    image = out / "arch/x86/boot/bzImage"
    for name in ("modules.builtin", "modules.builtin.modinfo", "System.map", "vmlinux"):
        if not (out / name).is_file():
            raise ValueError(f"Kernel output missing {name}")
    write_metadata(out / "build-meta.json",
                   {"status": "BUILT", "version": entry["version"],
                    "source_sha256": entry["source_sha256"], "source_dir": str(source),
                    "build_dir": str(out), "kernelrelease": release,
                    "fragment_sha256": sha256(fragment), "config_sha256": sha256(config),
                    "image": str(image), "image_sha256": sha256(image),
                    "build_jobs": jobs, "log": str(log_path)})
    print(f"BUILT kernel: {release}", flush=True)


def build_selftest(entry, source, jobs, log, log_path):
    """Build the unmodified upstream MM baseline, not another project workload."""
    out = ROOT / "build/selftests/mm"
    out.mkdir(parents=True, exist_ok=True)
    directory = source / "tools/testing/selftests/mm"
    binary = out / "migration"
    # Upstream's target omits some local headers from prerequisites. This small
    # baseline must be rebuilt before stamping hashes of all its current inputs.
    argv = ["make", "-B", "-C", directory, f"OUTPUT={out}", f"-j{jobs}", binary]
    run(argv, ROOT, log, timeout=180)
    inputs = [directory / name for name in (
        "migration.c", "vm_util.c", "vm_util.h", "thp_settings.c", "thp_settings.h",
        "Makefile", "check_config.sh", "../kselftest.h", "../kselftest_harness.h")]
    write_metadata(out / "build-meta.json", {
        "status": "BUILT", "kernel_version": entry["version"],
        "source_sha256": entry["source_sha256"], "binary_sha256": sha256(binary),
        "inputs_sha256": {str(p.resolve().relative_to(source)): sha256(p) for p in inputs},
        "argv": [str(x) for x in argv], "log": str(log_path)})
    print(f"BUILT upstream MM migration selftest: {binary}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", choices=["qemu", "kernel", "selftest", "all"])
    parser.add_argument("--jobs", type=int, default=2)
    args = parser.parse_args()
    if sys.platform != "linux" or os.geteuid() == 0:
        parser.error("Run the builder as the normal Linux/WSL project user")
    if not 1 <= args.jobs <= 4:
        parser.error("--jobs must be 1..4 for the 8 GiB WSL budget")
    try:
        lock = json.loads((ROOT / "configs/versions.lock.json").read_text(encoding="utf-8"))
        names = ["qemu", "linux"] if args.target == "all" else [
            "linux" if args.target in {"kernel", "selftest"} else "qemu"]
        entries = {name: validated_entry(lock, name) for name in names}
        build = ROOT / "build"
        build.mkdir(exist_ok=True)
        logs = build / "logs"
        logs.mkdir(exist_ok=True)
        with (build / "heavy.lock").open("a") as heavy:
            try:
                fcntl.flock(heavy, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ValueError("Another build/VM holds build/heavy.lock") from exc
            for name in names:
                entry = entries[name]
                archive = download(entry, ROOT / "downloads")
                source = extract(archive, build / "sources", f"{name}-{entry['version']}",
                                 entry["source_sha256"])
                tag = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
                path = logs / f"{name}-{tag}.log"
                print(f"Building {name}; log: {path}", flush=True)
                with path.open("w", encoding="utf-8") as log:
                    builder = (build_selftest if args.target == "selftest" else
                               build_qemu if name == "qemu" else build_kernel)
                    builder(entry, source, args.jobs, log, path)
        return 0
    except (ValueError, OSError, KeyError, subprocess.SubprocessError) as exc:
        print(f"BUILD FAILED: {exc}", file=sys.stderr)
        return 78 if isinstance(exc, (ValueError, KeyError)) else 1


if __name__ == "__main__":
    raise SystemExit(main())
