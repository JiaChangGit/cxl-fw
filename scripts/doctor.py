#!/usr/bin/env python3
"""Probe Host prerequisites without booting a Guest or changing configuration."""
import argparse
import datetime
import errno
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
TOOLS = (
    "git", "gcc", "g++", "cmake", "ctest", "ninja", "meson", "make",
    "python3", "gdb", "strace", "numactl", "clang-format", "shellcheck",
    "pkg-config", "bison", "flex", "qemu-img", "debootstrap", "ccache", "jq",
)


def probe():
    report = {
        "schema_version": 1,
        "timestamp_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "host": {"system": platform.system(), "kernel": platform.release()},
        "tools": {}, "packages": {}, "resources": {},
        "kvm": {"status": "NOT_RUN"}, "errors": [],
        "guest_boot": "NOT_RUN", "project_gates": "NOT_RUN",
    }
    errors = report["errors"]
    if sys.platform != "linux":
        errors.append("Run this doctor inside Linux/WSL, not Windows Python")
        report["host_ready"] = False
        return report

    for name in TOOLS:
        path = shutil.which(name)
        item = {"path": path, "status": "MISSING"}
        report["tools"][name] = item
        if not path:
            errors.append(f"Missing executable: {name}")
            continue
        try:
            result = subprocess.run([path, "--version"], capture_output=True,
                                    text=True, timeout=10, check=False)
            lines = (result.stdout + "\n" + result.stderr).strip().splitlines()
            item.update(status="PASS" if result.returncode == 0 else "FAIL",
                        version=lines[0][:200] if lines else "", exit_code=result.returncode)
            if result.returncode:
                errors.append(f"Version probe failed: {name}")
        except (OSError, subprocess.TimeoutExpired) as exc:
            item.update(status="FAIL", reason=str(exc))
            errors.append(f"Version probe failed: {name}")

    try:
        package_file = ROOT / "configs" / "host-packages.txt"
        packages = [line.strip() for line in package_file.read_text().splitlines()
                    if line.strip() and not line.lstrip().startswith("#")]
        result = subprocess.run(
            ["dpkg-query", "-W", "-f=${binary:Package}\t${db:Status-Abbrev}\t${Version}\n",
             "--", *packages], capture_output=True, text=True, timeout=15, check=False)
        for line in result.stdout.splitlines():
            fields = line.split("\t")
            if len(fields) == 3 and fields[1].startswith("ii"):
                report["packages"][fields[0].split(":")[0]] = fields[2]
        missing = sorted(set(packages) - set(report["packages"]))
        report["missing_packages"] = missing
        errors.extend(f"Missing package: {name}" for name in missing)
    except (OSError, subprocess.TimeoutExpired) as exc:
        errors.append(f"Package probe failed: {exc}")

    try:
        free = shutil.disk_usage(ROOT).free
        mem = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
        available = int(mem["MemAvailable"].split()[0]) * 1024
        report["resources"] = {"disk_free_bytes": free, "memory_available_bytes": available,
                               "min_disk_bytes": 35 * 1024**3, "min_memory_bytes": 2048 * 1024**2}
        if free < 35 * 1024**3:
            errors.append("Less than 35 GiB free disk space")
        if available < 2048 * 1024**2:
            errors.append("Less than 2048 MiB available memory")
    except (OSError, KeyError, ValueError) as exc:
        errors.append(f"Resource probe failed: {exc}")

    kvm = report["kvm"]
    kvm["uid"] = os.geteuid()
    if os.geteuid() == 0:
        kvm.update(status="BLOCKED", reason="Run doctor as the normal project user")
        errors.append("Root cannot validate normal-user KVM access")
    else:
        try:
            import fcntl
            fd = os.open("/dev/kvm", os.O_RDWR | os.O_CLOEXEC)
            try:
                version = fcntl.ioctl(fd, 0xAE00, 0)  # KVM_GET_API_VERSION
                kvm["api_version"] = version
                if version != 12:
                    raise OSError(errno.EPROTO, f"Unexpected KVM API version: {version}")
                vmfd = fcntl.ioctl(fd, 0xAE01, 0)  # KVM_CREATE_VM，無 vCPU／記憶體
                os.close(vmfd)
                kvm.update(status="PASS", empty_vm_create="PASS")
            finally:
                os.close(fd)
        except OSError as exc:
            kvm.update(status="BLOCKED", errno=exc.errno,
                       errno_name=errno.errorcode.get(exc.errno, "UNKNOWN"), reason=str(exc))
            errors.append(f"KVM probe blocked: {exc.strerror}")
    report["host_ready"] = not errors
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, help="Write the JSON evidence report")
    args = parser.parse_args()
    report = probe()
    if args.report:
        try:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        except OSError as exc:
            print(f"ERROR: Unable to save report: {exc}", file=sys.stderr)
            return 78
    state = "READY" if report["host_ready"] else "BLOCKED"
    print(f"Host environment: {state}; KVM={report['kvm']['status']}; Guest boot=NOT_RUN")
    for error in report["errors"]:
        print(f"  {error}")
    if args.report:
        print(f"Report: {args.report}")
    return 0 if report["host_ready"] else 78


if __name__ == "__main__":
    sys.exit(main())
