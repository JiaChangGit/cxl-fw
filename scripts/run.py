#!/usr/bin/env python3
"""Run isolated Guest scenarios and verify real M0–M4 evidence."""
import argparse
import datetime
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
HEALTH_FAILURE = re.compile(r"\bKernel panic\b|\bOops:|\bBUG:|\bWARNING: CPU:|\bkernel BUG at\b|\bgeneral protection fault\b", re.I)
EXIT_CODES = {"PASS": 0, "FAIL": 1, "USAGE": 2, "BLOCKED": 78, "TIMEOUT": 124, "CRASHED": 125}


def sha256(path):
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256")
    return digest.hexdigest()


def save_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


class QMP:
    """One synchronous client; preserve asynchronous events and other response IDs."""
    def __init__(self, connection, log):
        self.connection, self.log = connection, log
        self.buffer, self.sequence = b"", 0
        self.events, self.pending = [], {}

    def record(self, direction, message):
        self.log.write(json.dumps({"direction": direction, "message": message}) + "\n")
        self.log.flush()

    def receive(self, deadline):
        while b"\n" not in self.buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("QMP response deadline")
            self.connection.settimeout(remaining)
            data = self.connection.recv(65536)
            if not data:
                raise ConnectionError("QMP connection closed")
            self.buffer += data
            if len(self.buffer) > 1024 * 1024:
                raise ValueError("QMP message exceeds 1 MiB")
        line, self.buffer = self.buffer.split(b"\n", 1)
        message = json.loads(line)
        if not isinstance(message, dict):
            raise ValueError("QMP message must be an object")
        self.record("receive", message)
        return message

    def command(self, name, deadline):
        self.sequence += 1
        identity = f"request-{self.sequence}"
        request = {"execute": name, "id": identity}
        self.record("send", request)
        self.connection.settimeout(max(0.01, deadline - time.monotonic()))
        self.connection.sendall(json.dumps(request).encode() + b"\n")
        while True:
            message = self.pending.pop(identity, None)
            if message is None:
                message = self.receive(deadline)
            if "event" in message:
                self.events.append(message)
            elif message.get("id") != identity:
                if not isinstance(message.get("id"), (str, int)) or len(self.pending) >= 128:
                    raise ValueError("Uncorrelated QMP response")
                self.pending[message["id"]] = message
            elif "error" in message:
                raise RuntimeError(f"QMP {name}: {message['error']}")
            elif "return" in message:
                return message["return"]
            else:
                raise ValueError("QMP response has neither return nor error")

    @classmethod
    def connect(cls, path, log, deadline, process):
        while True:
            if process.poll() is not None:
                raise ChildProcessError("QEMU exited before QMP connected")
            connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                connection.settimeout(min(0.5, max(0.01, deadline - time.monotonic())))
                connection.connect(str(path))
                client = cls(connection, log)
                if "QMP" not in client.receive(deadline):
                    raise ValueError("Missing QMP greeting")
                client.command("qmp_capabilities", deadline)
                return client
            except (FileNotFoundError, ConnectionRefusedError):
                connection.close()
                if time.monotonic() >= deadline:
                    raise TimeoutError("QMP connection deadline") from None
                time.sleep(0.05)
            except BaseException:
                connection.close()
                raise


def choose_accel(requested):
    if requested == "tcg":
        return "tcg", "Explicit TCG profile"
    try:
        fd = os.open("/dev/kvm", os.O_RDWR | os.O_CLOEXEC)
        try:
            if fcntl.ioctl(fd, 0xAE00, 0) != 12:
                raise OSError(errno.EPROTO, "Unexpected KVM API version")
            vm = fcntl.ioctl(fd, 0xAE01, 0)
            os.close(vm)
        finally:
            os.close(fd)
        return "kvm", "Normal-user KVM API and empty-VM probe passed"
    except OSError as exc:
        reason = f"KVM probe failed: {errno.errorcode.get(exc.errno, 'UNKNOWN')}: {exc}"
        if requested == "kvm":
            raise OSError(reason) from exc
        return "tcg", reason


class GuestBlocked(RuntimeError):
    """The correlated Guest reported a missing required capability."""


def validate_guest(shared, console, run_id, expected_kernel, scenario="boot"):
    failure = HEALTH_FAILURE.search(console)
    if failure:
        raise ValueError(f"Kernel health failure: {failure.group(0)}")
    data = json.loads((shared / "guest.json").read_text(encoding="utf-8"))
    if not isinstance(data, dict) or type(data.get("schema_version")) is not int:
        raise ValueError("Guest report must be a versioned JSON object")
    for key, expected in (("schema_version", 1), ("run_id", run_id),
                          ("scenario", scenario),
                          ("kernel_release", expected_kernel)):
        if data.get(key) != expected:
            raise ValueError(f"Guest {key} does not match {expected!r}")
    try:
        uuid.UUID(data["boot_id"])
    except (KeyError, ValueError, TypeError, AttributeError) as exc:
        raise ValueError("Missing or invalid Guest boot_id") from exc
    tools = data.get("tools")
    if not isinstance(tools, dict):
        raise ValueError("Missing Guest tools")
    for name in ("cxl", "daxctl", "ndctl"):
        entry = tools.get(name)
        if not isinstance(entry, dict) or not entry.get("path") or not entry.get("version"):
            raise ValueError(f"Missing Guest {name} path/version")
    cmdline = data.get("cmdline")
    if not isinstance(cmdline, str) or not all(flag in cmdline.split() for flag in (
            "root=/dev/vda", "transparent_hugepage=never",
            "numa_balancing=disable", "memhp_default_state=offline")):
        raise ValueError("Guest cmdline does not match the controlled boot profile")
    status = data.get("status")
    if status not in {"PASS", "FAIL", "BLOCKED"}:
        raise ValueError("Unknown Guest status")
    marker = rf"\bTIERLAB_DONE {re.escape(run_id)} {status}(?:\s|$)"
    if not re.search(marker, console):
        raise ValueError("Missing correlated console completion marker")
    if re.search(rf"\bTIERLAB_DONE {re.escape(run_id)} FAIL(?:\s|$)", console):
        raise ValueError("Guest reported a console failure")
    dmesg = (shared / "dmesg.log").read_text(encoding="utf-8")
    if not dmesg.strip() or HEALTH_FAILURE.search(dmesg):
        raise ValueError("Guest dmesg is empty or contains a kernel health failure")
    if status == "BLOCKED":
        raise GuestBlocked(data.get("reason", "Guest prerequisite blocked"))
    if status != "PASS":
        raise ValueError(data.get("reason", "Guest scenario failed"))
    return data


def stop_owned_process(process, client):
    """Bounded cleanup, only for the Popen instance created by this runner."""
    if process.poll() is not None:
        return
    for command, wait in (("system_powerdown", 5), ("quit", 3)):
        if client is not None:
            try:
                client.command(command, time.monotonic() + 1)
            except (OSError, ValueError, RuntimeError, ConnectionError, TimeoutError):
                pass  # 清理仍須走到本 runner 擁有的程序終止。
        try:
            process.wait(timeout=wait if client else 0.1)
            return
        except subprocess.TimeoutExpired:
            pass
    process.terminate()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)


def qemu_argv(binary, kernel, overlay, shared, console, qmp_socket, accel, cmdline):
    if len(os.fsencode(qmp_socket)) >= 104:
        raise ValueError("QMP socket path is too long; use a shorter project workspace path")
    return [
        str(binary), "-machine", f"q35,cxl=on,accel={accel}", "-cpu",
        "host" if accel == "kvm" else "max", "-smp", "4", "-m", "3G",
        "-object", "memory-backend-ram,id=dram0,size=3G",
        "-numa", "node,nodeid=0,memdev=dram0",
        "-object", "memory-backend-ram,id=vmem0,share=on,size=1G",
        "-device", "pxb-cxl,bus_nr=12,bus=pcie.0,id=cxl.1",
        "-device", "cxl-rp,port=0,bus=cxl.1,id=rp0,chassis=0,slot=2",
        "-device", "cxl-type3,bus=rp0,volatile-memdev=vmem0,id=cxl-vmem0,sn=0x1",
        "-machine", "cxl-fmw.0.targets.0=cxl.1,cxl-fmw.0.size=4G",
        "-kernel", str(kernel), "-append", cmdline,
        "-drive", f"file={overlay},if=none,id=rootfs,format=qcow2",
        "-device", "virtio-blk-pci,drive=rootfs,bus=pcie.0",
        "-fsdev", f"local,id=fs0,path={shared},security_model=none",
        "-device", "virtio-9p-pci,fsdev=fs0,mount_tag=host0,bus=pcie.0",
        "-serial", f"file:{console}", "-qmp", f"unix:{qmp_socket},server=on,wait=off",
        "-display", "none", "-vga", "none", "-nic", "none", "-no-reboot",
    ]


def run_once(root, artifacts, accel, reason, timeout, scenario="boot"):
    run_id = uuid.uuid4().hex
    directory = root / "results/runs" / run_id
    shared = directory / "shared"
    shared.mkdir(parents=True, exist_ok=False)
    config = {"schema_version": 1, "run_id": run_id, "scenario": scenario}
    save_json(shared / "config.json", config)
    console_path, qmp_path = directory / "console.log", directory / "qmp.sock"
    overlay = directory / "rootfs-overlay.qcow2"
    pinned_base = directory / "base.ext4"
    result = {"schema_version": 1, "run_id": run_id, "scenario": scenario,
              "status": "FAIL", "evidence_basis": "guest_kernel_measured"}
    metadata = {"schema_version": 1, "run_id": run_id, "accel": accel,
                "accel_reason": reason, "input_sha256": artifacts["hashes"],
                "config_sha256": sha256(shared / "config.json")}
    save_json(directory / "config.json", config)
    process, client = None, None
    save_json(directory / "meta.json", metadata)
    try:
        # Hard-link the read-only inode so a later base rebuild cannot change this run.
        os.link(artifacts["base"], pinned_base)
        if scenario in {"migrate", "policy", "offline", "trace"}:
            shutil.copy2(artifacts["tierlab"], shared / "tierlab")
            if sha256(shared / "tierlab") != artifacts["hashes"]["build/tierlab/tierlab"]:
                raise ValueError("Copied Guest binary differs from input provenance")
            elf = subprocess.run(["readelf", "--program-headers", "--dynamic", str(shared / "tierlab")],
                                 capture_output=True, text=True, check=True, timeout=10)
            (shared / "tierlab-elf.txt").write_text(elf.stdout, encoding="utf-8")
        if scenario == "trace":
            for key, name in (("baseline", "upstream-migration"),
                              ("baseline_meta", "upstream-build.json"),
                              ("kernel_config", "kernel.config")):
                shutil.copy2(artifacts[key], shared / name)
                expected = artifacts["hashes"][str(artifacts[key].relative_to(root))]
                if sha256(shared / name) != expected:
                    raise ValueError(f"Copied trace input differs from provenance: {name}")
        subprocess.run([artifacts["qemu_img"], "create", "-f", "qcow2", "-F", "raw",
                        "-b", str(pinned_base), str(overlay)],
                       check=True, capture_output=True, text=True, timeout=15)
        argv = qemu_argv(artifacts["qemu"], artifacts["kernel"], overlay, shared,
                         console_path, qmp_path, accel, artifacts["cmdline"])
        metadata["argv"] = argv
        save_json(directory / "meta.json", metadata)
        deadline = time.monotonic() + timeout
        with (directory / "qemu.stderr.log").open("w", encoding="utf-8") as stderr, \
                (directory / "qmp.jsonl").open("w", encoding="utf-8") as qmp_log:
            process = subprocess.Popen(argv, stdin=subprocess.DEVNULL,
                                       stdout=stderr, stderr=stderr)
            metadata["qemu_pid"] = process.pid
            save_json(directory / "meta.json", metadata)
            try:
                client = QMP.connect(qmp_path, qmp_log, min(deadline, time.monotonic() + 15), process)
                metadata["qmp_initial_status"] = client.command("query-status", deadline)
                save_json(directory / "meta.json", metadata)
                process.wait(timeout=max(0.01, deadline - time.monotonic()))
                if process.returncode != 0:
                    raise ChildProcessError(f"QEMU exited with {process.returncode}")
                console = console_path.read_text(encoding="utf-8", errors="replace")
                guest = validate_guest(shared, console, run_id, artifacts["kernel_version"], scenario)
                if scenario in {"migrate", "policy", "offline", "trace"}:
                    if scenario == "migrate":
                        from validate_migration import validate_migration
                        verified = validate_migration(shared, run_id)
                        evidence_name = "migration"
                    elif scenario == "policy":
                        from validate_policy import validate_policy
                        verified = validate_policy(shared, run_id)
                        evidence_name = "policy"
                    elif scenario == "offline":
                        from validate_offline import validate_offline
                        verified = validate_offline(shared, run_id)
                        evidence_name = "offline"
                    else:
                        from validate_trace import validate_trace, trace_negative_controls
                        verified = validate_trace(shared, run_id)
                        verified["negative_controls"] = trace_negative_controls(shared, run_id, verified)
                        save_json(directory / "trace_negative_controls.json", {
                            "schema_version": 1, "run_id": run_id, "scenario": "trace",
                            "evidence_basis": "modeled", "controls": verified["negative_controls"]})
                        evidence_name = "trace"
                    save_json(directory / f"{evidence_name}_verified.json", verified)
                    shutil.copyfile(shared / "topology.json", directory / "topology.json")
                    result[evidence_name] = verified
                save_json(directory / "guest.json", guest)
                shutil.copyfile(shared / "dmesg.log", directory / "dmesg.log")
                result.update(status="PASS", boot_id=guest["boot_id"])
            finally:
                stop_owned_process(process, client)
                if client:
                    client.connection.close()
                metadata["qemu_exit_code"] = process.returncode
                save_json(directory / "meta.json", metadata)
    except (TimeoutError, subprocess.TimeoutExpired) as exc:
        result.update(status="TIMEOUT", reason=str(exc))
    except GuestBlocked as exc:
        result.update(status="BLOCKED", reason=str(exc))
    except ChildProcessError as exc:
        result.update(status="CRASHED", reason=str(exc))
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.CalledProcessError,
            ConnectionError) as exc:
        result["reason"] = str(exc)
    finally:
        if process is not None and process.poll() is None:
            stop_owned_process(process, None)
        if process is not None and result["status"] == "FAIL" and process.returncode not in (None, 0):
            result.update(status="CRASHED", reason=f"QEMU exited with {process.returncode}; {result.get('reason', '')}")
        if console_path.is_file():
            console = console_path.read_text(encoding="utf-8", errors="replace")
            failure = HEALTH_FAILURE.search(console)
            if failure:
                result.update(status="CRASHED", reason=f"Kernel health failure: {failure.group(0)}")
        result["evidence"] = [
            str(p.relative_to(root)) for p in sorted(directory.rglob("*"))
            if p.is_file() and p.name != "summary.json"]
        save_json(directory / "summary.json", result)
    print(f"{result['status']}: {scenario} {run_id}" + (f": {result['reason']}" if "reason" in result else ""))
    return result


def load_artifacts(root, scenario="boot"):
    lock_path = root / "configs/versions.lock.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    for name, version in (("qemu", "11.1.0"), ("linux", "6.18.56")):
        entry = lock[name]
        if entry.get("version") != version or not re.fullmatch(r"[a-fA-F0-9]{64}", str(entry.get("source_sha256", ""))):
            raise ValueError(f"Missing verified source pin for {name}")
    qemu = root / "install/qemu/bin/qemu-system-x86_64"
    kernel = root / "build/kernel/arch/x86/boot/bzImage"
    base = root / "images/rootfs.ext4"
    kernel_config = root / "build/kernel/.config"
    rootfs_meta = root / "images/rootfs.meta.json"
    packages = root / "images/rootfs.packages.tsv"
    for path in (qemu, kernel, base, kernel_config, rootfs_meta, packages):
        if not path.is_file():
            raise OSError(f"Required artifact missing: {path}")
    version = subprocess.run([str(qemu), "--version"], capture_output=True,
                             text=True, timeout=10, check=True).stdout
    found = re.search(r"QEMU emulator version (\S+)", version)
    if found is None or found.group(1) != lock["qemu"]["version"]:
        raise ValueError("QEMU binary version differs from the source lock")
    qemu_img = shutil.which("qemu-img")
    if not qemu_img:
        raise OSError("qemu-img is missing")
    cmdline_path = root / "configs/cmdline.txt"
    cmdline = " ".join(cmdline_path.read_text(encoding="utf-8").split())
    hashes = {str(p.relative_to(root)): sha256(p) for p in (
        lock_path, qemu, kernel, base, kernel_config, cmdline_path, rootfs_meta, packages)}
    image_meta = json.loads(rootfs_meta.read_text(encoding="utf-8"))
    for key, expected in (("image_sha256", hashes[str(base.relative_to(root))]),
                          ("kernel_release", lock["linux"]["version"]),
                          ("kernel_config_sha256", hashes[str(kernel_config.relative_to(root))]),
                          ("package_manifest_sha256", hashes[str(packages.relative_to(root))])):
        if image_meta.get(key) != expected:
            raise ValueError(f"Rootfs provenance mismatch: {key}")
    guest_inputs = image_meta.get("guest_inputs", {})
    for name in ("guest/run.sh", "guest/cxl_setup.py", "guest/cxl_offline.py", "guest/trace.py", "guest/tierlab.service"):
        path = root / name
        if not path.is_file() or guest_inputs.get(name) != sha256(path):
            raise ValueError(f"Guest source changed; rebuild rootfs: {name}")
    artifacts = {"qemu": qemu, "kernel": kernel, "base": base, "qemu_img": qemu_img,
                 "cmdline": cmdline, "kernel_version": lock["linux"]["version"], "hashes": hashes}
    if scenario in {"migrate", "policy", "offline", "trace"}:
        binary = root / "build/tierlab/tierlab"
        if not binary.is_file():
            raise OSError(f"{scenario} requires the built Guest binary; run make tierlab")
        artifacts["tierlab"] = binary
        hashes["build/tierlab/tierlab"] = sha256(binary)
    if scenario == "trace":
        binary = root / "build/selftests/mm/migration"
        meta = root / "build/selftests/mm/build-meta.json"
        if not binary.is_file() or not meta.is_file():
            raise OSError("trace requires the upstream baseline; run make upstream-selftest")
        data = json.loads(meta.read_text(encoding="utf-8"))
        if (data.get("status") != "BUILT" or data.get("kernel_version") != lock["linux"]["version"]
                or data.get("source_sha256") != lock["linux"]["source_sha256"]
                or data.get("binary_sha256") != sha256(binary)):
            raise ValueError("Upstream selftest provenance mismatch; rebuild upstream-selftest")
        source = root / "build/sources/linux-6.18.56"
        expected_inputs = {"tools/testing/selftests/mm/" + n for n in (
            "migration.c", "vm_util.c", "vm_util.h", "thp_settings.c", "thp_settings.h", "Makefile", "check_config.sh")}
        expected_inputs |= {"tools/testing/selftests/" + n for n in ("kselftest.h", "kselftest_harness.h")}
        if set(data.get("inputs_sha256", {})) != expected_inputs:
            raise ValueError("Upstream selftest source manifest is incomplete")
        for name, digest in data["inputs_sha256"].items():
            if sha256(source / name) != digest:
                raise ValueError(f"Upstream selftest source changed: {name}")
        for path in (binary, meta):
            hashes[str(path.relative_to(root))] = sha256(path)
        artifacts.update(baseline=binary, baseline_meta=meta, kernel_config=kernel_config)
    return artifacts


def show_demo(root):
    """Replay saved real evidence without rebuilding or starting a VM."""
    from validate_migration import validate_migration
    from validate_policy import validate_policy
    from validate_offline import validate_offline
    from validate_trace import validate_trace
    artifacts = load_artifacts(root, "trace")
    selected = {}
    for filename, scenario, gates, validator in (
            ("G0", "boot", ["G0"], None),
            ("M1", "migrate", ["G1", "G2"], validate_migration),
            ("M2", "policy", ["G3"], validate_policy),
            ("M3", "offline", ["G4"], validate_offline),
            ("M4", "trace", ["G5"], validate_trace)):
        gate = json.loads((root / f"results/gates/{filename}.json").read_text())
        if gate.get("status") != "PASS" or gate.get("gates") != dict.fromkeys(gates, "PASS"):
            raise ValueError(f"{filename} has no complete PASS evidence; run make check")
        for name, digest in gate["input_sha256"].items():
            if artifacts["hashes"].get(name) != digest:
                raise ValueError(f"{filename} evidence is stale for {name}; run make check")
        runs = gate["runs"]
        if not runs or (scenario == "boot" and
                        (len(runs) < 3 or len({r.get('boot_id') for r in runs}) != len(runs))):
            raise ValueError(f"{filename} lacks clean boot evidence")
        for result in runs:
            run_id = result["run_id"]
            if not re.fullmatch(r"[a-f0-9]{32}", run_id) or result.get("status") != "PASS":
                raise ValueError(f"{filename} has an invalid run")
            directory = root / "results/runs" / run_id
            validate_guest(directory / "shared", (directory / "console.log").read_text(),
                           run_id, artifacts["kernel_version"], scenario)
            if validator:
                verified = validator(directory / "shared", run_id)
                if verified.get("status") != "PASS":
                    raise ValueError(f"{filename} replay failed")
                selected[filename] = verified
        print(f"PASS: {'/'.join(gates)} {scenario}; evidence=results/runs/{runs[-1]['run_id']}")
    trace_run = json.loads((root / "results/gates/M4.json").read_text())["runs"][-1]["run_id"]
    shared = root / "results/runs" / trace_run / "shared"
    topology = json.loads((shared / "topology.json").read_text())
    print(f"Topology: DRAM node {topology['fast_node']}, CXL node {topology['slow_node']}")
    batch = json.loads((shared / "trace_batches.jsonl").read_text().splitlines()[1])
    print(f"Request: {batch['name']}; rc={batch['syscall_rc']} errno={batch['syscall_errno']}; "
          f"integrity_failures={batch['integrity_failures']}")
    for line in (shared / "trace.log").read_text().splitlines()[:4]:
        print(line)
    for experiment in selected["M2"]["experiments"]:
        if experiment["baseline"] == "B2":
            print(f"Policy B2 seed={experiment['seed']}: "
                  f"last10 ready HFR={experiment['last10_ready_hfr_median']:.3f}; "
                  f"last10 causal AWFR={experiment['awfr_last10']:.3%}; "
                  f"recovery epoch={experiment['recovery_epoch']}")
    invalid = selected["M4"]["batches"][-1]
    print(f"Invalid target: rc={invalid['syscall_rc']} errno={invalid['syscall_errno']}; "
          f"kernel migration events={invalid['trace_start_events']}")
    from validate_trace import trace_negative_controls
    controls = trace_negative_controls(shared, trace_run, selected["M4"])
    selected["M4"]["negative_controls"] = controls
    print("Trace negative controls: " + ", ".join(f"{c['name']}={c['status']}" for c in controls))
    save_json(root / "results/demo.json", {
        "schema_version": 1, "status": "PASS", "mode": "saved_evidence_replay",
        "evidence_basis": "guest_kernel_measured", "verified": selected})
    print("PASS: G0-G5 saved evidence replay; results/demo.json")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scenario", choices=["boot", "migrate", "policy", "offline", "trace", "demo"])
    parser.add_argument("--repeat", type=int)
    parser.add_argument("--accel", choices=["auto", "kvm", "tcg"], default="auto")
    parser.add_argument("--timeout", type=int)
    args = parser.parse_args()
    if args.scenario == "demo":
        try:
            return show_demo(ROOT)
        except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as exc:
            print(f"DEMO FAILED: {exc}", file=sys.stderr)
            return 1
    if args.repeat is None:
        args.repeat = 3 if args.scenario == "boot" else 1
    if args.timeout is None:
        args.timeout = 900 if args.scenario == "policy" else (300 if args.scenario == "offline" else 180)
    if not 1 <= args.repeat <= 20 or not 10 <= args.timeout <= 1800:
        parser.error("repeat must be 1..20; timeout must be 10..1800 seconds")
    gate_dir = ROOT / "results/gates"
    gate_dir.mkdir(parents=True, exist_ok=True)
    is_boot = args.scenario == "boot"
    milestone, gate_file, gate_ids = {
        "boot": ("M0", "G0.json", ["G0"]),
        "migrate": ("M1", "M1.json", ["G1", "G2"]),
        "policy": ("M2", "M2.json", ["G3"]),
        "offline": ("M3", "M3.json", ["G4"]),
        "trace": ("M4", "M4.json", ["G5"]),
    }[args.scenario]
    gate_path = gate_dir / gate_file
    summary = {"schema_version": 1, "milestone": milestone,
               "scenario": args.scenario,
               "timestamp_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
               "status": "BLOCKED", "gates": {name: "NOT_RUN" for name in gate_ids}, "runs": []}
    try:
        if sys.platform != "linux" or os.geteuid() == 0:
            raise OSError("Run the VM as the normal project user inside Linux/WSL")
        (ROOT / "build").mkdir(exist_ok=True)
        with (ROOT / "build/heavy.lock").open("a+") as resource_lock:
            try:
                fcntl.flock(resource_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise OSError("A heavy build or VM run already holds build/heavy.lock") from None
            accel, reason = choose_accel(args.accel)
            artifacts = load_artifacts(ROOT, args.scenario)
            summary.update(accel=accel, accel_reason=reason, input_sha256=artifacts["hashes"])
            for _ in range(args.repeat):
                result = run_once(ROOT, artifacts, accel, reason, args.timeout, args.scenario)
                summary["runs"].append(result)
                if result["status"] != "PASS":
                    break
            results = summary["runs"]
            status = results[-1]["status"]
            boot_ids = {r.get("boot_id") for r in results}
            if status == "PASS" and len(boot_ids) != len(results):
                status = "FAIL"
                summary["reason"] = "Repeated Guest boot_id; clean boot evidence is invalid"
            summary["status"] = status
            for name in gate_ids:
                summary["gates"][name] = ("NOT_RUN" if is_boot and status == "PASS"
                                         and len(results) < 3 else status)
            if is_boot and len(results) < 3 and status == "PASS":
                summary["reason"] = "At least three clean boots are required for G0"
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        summary["reason"] = str(exc)
    save_json(gate_path, summary)
    print(f"{args.scenario} scenario: {summary['status']}; gates={summary['gates']}")
    if "reason" in summary:
        print(summary["reason"])
    return EXIT_CODES[summary["status"]]


if __name__ == "__main__":
    raise SystemExit(main())
