#!/usr/bin/env python3
"""Bring one volatile CXL region online in a verified QEMU Guest."""
import argparse
import gzip
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys

SYS = Path("/sys")
U64 = 1 << 64


def objects(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from objects(child)


def one(values, label):
    values = list(values)
    if len(values) != 1:
        raise ValueError(f"Expected one {label}, found {len(values)}")
    return values[0]


def interval(start, size):
    if type(start) is not int or type(size) is not int or not 0 <= start < U64 or size <= 0 or start + size > U64:
        raise ValueError("Invalid or overflowing physical resource range")
    return start, start + size


def contained_blocks(start, size, blocks):
    low, high = interval(start, size)
    selected = []
    for block in blocks:
        first, last = interval(block["start"], block["size"])
        if first < high and last > low:
            if first < low or last > high:
                raise ValueError("Memory block only partially overlaps DAX range")
            selected.append(block)
    selected.sort(key=lambda b: b["start"])
    cursor = low
    for block in selected:
        if block["start"] != cursor:
            raise ValueError("Hole or overlapping blocks in DAX range")
        cursor += block["size"]
    if cursor != high:
        raise ValueError("DAX range has missing memory blocks")
    return selected


def movable_state(block):
    if block["state"] == "offline":
        if "Movable" not in block["valid_zones"].split():
            raise ValueError("Offline block does not support Movable")
    elif block["state"] != "online" or block["valid_zones"].split() != ["Movable"]:
        raise ValueError("Refusing a non-Movable or transitional online block")


def verify_ownership(iomem, name, start, size):
    low, high = interval(start, size)
    resources = []
    for line in iomem.splitlines():
        match = re.fullmatch(r"\s*([0-9a-fA-F]+)-([0-9a-fA-F]+) : (.+)", line)
        if match:
            first, last = int(match[1], 16), int(match[2], 16) + 1
            resources.append({"start": first, "size": last - first, "label": match[3]})
    one((r for r in resources if r["label"] == name
         and interval(r["start"], r["size"]) == (low, high)), "DAX resource owner")
    contained_blocks(start, size, [r for r in resources if r["label"] == "System RAM (kmem)"])


def fast_node(nodes, blocks, slow, start, size):
    low, high = interval(start, size)
    ordinary = {"DMA", "DMA32", "Normal"}
    return one((n["node"] for n in nodes if n["node"] != slow and n["cpulist"]
                and any(b["node"] == n["node"] and b["state"] == "online"
                        and set(b["valid_zones"].split()) & ordinary
                        and (b["start"] + b["size"] <= low or b["start"] >= high)
                        for b in blocks)), "CPU-backed DRAM node")


def read(path):
    return path.read_text().strip()


def integer(path):
    value = read(path)
    return int(value, 16 if value.startswith("0x") else 10)


def guest_identity():
    if platform.release() != "6.18.56" or "microsoft" in platform.release().lower():
        raise ValueError("Refusing writes outside the pinned Guest kernel")
    vendor = read(SYS / "class/dmi/id/sys_vendor")
    product = read(SYS / "class/dmi/id/product_name")
    if "QEMU" not in vendor and "QEMU" not in product:
        raise ValueError("Refusing writes outside QEMU")
    if os.geteuid() != 0:
        raise ValueError("Guest CXL setup requires Guest root")


class Setup:
    def __init__(self, output, run_id):
        self.output, self.run_id = output, run_id
        self.raw_files = {}

    def publish(self, name, data):
        path = self.output / name
        wrapper = {"schema_version": 1, "run_id": self.run_id, "scenario": "migrate", "data": data}
        temp = path.with_suffix(path.suffix + ".tmp")
        with temp.open("w") as stream:
            json.dump(wrapper, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temp.replace(path)

    def command(self, argv, allow_error=False):
        result = subprocess.run(argv, capture_output=True, text=True, timeout=60)
        record = {"schema_version": 1, "run_id": self.run_id, "scenario": "migrate",
                  "argv": argv, "exit_code": result.returncode,
                  "stdout": result.stdout, "stderr": result.stderr}
        with (self.output / "topology-commands.jsonl").open("a") as stream:
            stream.write(json.dumps(record) + "\n")
        if result.returncode and not allow_error:
            raise RuntimeError(f"{argv[0]} {argv[1]} failed: {result.stderr.strip()}")
        return result.stdout

    def listing(self, key, filename, argv):
        data = json.loads(self.command(argv) or "[]")
        self.publish(filename, data)
        self.raw_files[key] = filename
        return data

    def check_help(self, tool, command, options):
        self.command([tool, command, "--help"], allow_error=True)
        # Minimal rootfs has man pages but no man viewer; inspect the installed manual.
        path = Path("/usr/share/man/man1") / f"{tool}-{command}.1.gz"
        text = gzip.decompress(path.read_bytes()).decode().replace("\\-", "-")
        if not all(option in text for option in options):
            raise ValueError(f"Installed {tool} manual lacks required options")
        (self.output / f"{tool}-{command}.manual.txt").write_text(text)

    def memory_blocks(self):
        root = SYS / "devices/system/memory"
        block_size = int(read(root / "block_size_bytes"), 16)
        blocks = []
        for path in root.glob("memory[0-9]*"):
            if not re.fullmatch(r"memory\d+", path.name):
                continue
            index = int(read(path / "phys_index"), 16)
            nodes = sorted(p.name for p in path.glob("node[0-9]*") if re.fullmatch(r"node\d+", p.name))
            blocks.append({"name": path.name, "start": index * block_size, "size": block_size,
                           "phys_index": index, "state": read(path / "state"),
                           "valid_zones": read(path / "valid_zones"), "node_links": nodes,
                           "node": int(nodes[0][4:]) if len(nodes) == 1 else None,
                           "zone": read(path / "valid_zones")})
        return blocks

    def snapshot(self, region, dax, blocks):
        rp, dp = SYS / "bus/cxl/devices" / region, SYS / "bus/dax/devices" / dax
        nodes = [{"node": int(p.name[4:]), "cpulist": read(p / "cpulist"),
                  "meminfo": read(p / "meminfo")} for p in
                 sorted((SYS / "devices/system/node").glob("node[0-9]*"))]
        tiers = {p.name: read(p / "nodelist") for p in
                 sorted((SYS / "devices/virtual/memory_tiering").glob("memory_tier*"))}
        return {"auto_online_blocks": read(SYS / "devices/system/memory/auto_online_blocks"),
                "region": {"name": region, "resource": integer(rp / "resource"),
                           "size": integer(rp / "size"), "mode": read(rp / "mode"),
                           "interleave_ways": integer(rp / "interleave_ways")},
                "dax": {"name": dax, "resource": integer(dp / "resource"),
                        "size": integer(dp / "size"), "target_node": integer(dp / "target_node"),
                        "driver": (dp / "driver").resolve().name},
                "blocks": blocks, "nodes": nodes, "memory_tiers": tiers}

    def execute(self):
        guest_identity()
        for tool, sub, options in (("cxl", "create-region", ["--type", "--memdevs", "--decoder"]),
                                    ("daxctl", "reconfigure-device", ["--mode", "--no-online"])):
            self.check_help(tool, sub, options)
        mems = self.listing("memdevs", "cxl-memdevs.json", ["cxl", "list", "-M"])
        mem = one((o for o in objects(mems) if "memdev" in o), "CXL memdev")
        if not re.fullmatch(r"mem\d+", mem["memdev"]) or mem.get("ram_size", 0) < 1 << 30 or mem.get("pmem_size", 0) != 0:
            raise ValueError("Expected one volatile-only Type-3 with at least 1 GiB")
        decoders = self.listing("decoders", "cxl-decoders.json", ["cxl", "list", "-D", "-d", "root"])
        candidates = []
        for path in (SYS / "bus/cxl/devices").glob("decoder*"):
            if (path.name in {o.get("decoder") for o in objects(decoders)}
                    and (path / "cap_ram").exists() and integer(path / "cap_ram") == 1
                    and integer(path / "cap_type3") == 1):
                reachable = json.loads(self.command(["cxl", "list", "-M", "-d", path.name]) or "[]")
                if any(o.get("memdev") == mem["memdev"] for o in objects(reachable)):
                    candidates.append(path.name)
        decoder = one(candidates, "reachable RAM root decoder")
        (SYS / "devices/system/memory/auto_online_blocks").write_text("offline\n")
        regions = json.loads(self.command(["cxl", "list", "-R"]) or "[]")
        regions = [o for o in objects(regions) if "region" in o]
        if not regions:
            self.command(["cxl", "create-region", "-t", "ram", "-m", "-d", decoder, "-w", "1", mem["memdev"]])
        listed = self.listing("regions", "cxl-regions.json", ["cxl", "list", "-R"])
        region = one((o for o in objects(listed) if "region" in o), "CXL region")["region"]
        if not re.fullmatch(r"region\d+", region):
            raise ValueError("Invalid region identifier")
        rp = SYS / "bus/cxl/devices" / region
        if ((SYS / "bus/cxl/devices" / decoder).resolve() not in rp.resolve().parents
                or read(rp / "mode") != "ram" or integer(rp / "interleave_ways") != 1):
            raise ValueError("Expected committed 1-way RAM region")
        dax_candidates = [p for p in (SYS / "bus/dax/devices").glob("dax*")
                          if re.fullmatch(r"dax\d+\.\d+", p.name) and rp.resolve() in p.resolve().parents]
        dp = one(dax_candidates, "DAX device owned by CXL region")
        start, size, slow = integer(dp / "resource"), integer(dp / "size"), integer(dp / "target_node")
        if slow < 0 or interval(start, size) != interval(integer(rp / "resource"), integer(rp / "size")):
            raise ValueError("Ambiguous DAX/region resource or target node")
        before_dax = json.loads(self.command(["daxctl", "list", "-D"]) or "[]")
        device = one((o for o in objects(before_dax) if o.get("chardev") == dp.name), "DAX listing")
        if device.get("mode") == "devdax":
            self.command(["daxctl", "reconfigure-device", "--mode=system-ram", "--no-online", dp.name])
        elif device.get("mode") != "system-ram":
            raise ValueError("Unsupported DAX mode")
        blocks = contained_blocks(start, size, self.memory_blocks())
        if (dp / "driver").resolve().name != "kmem":
            raise ValueError("DAX is not bound to kmem")
        iomem = read(Path("/proc/iomem"))
        (self.output / "iomem.txt").write_text(iomem + "\n")
        self.raw_files["iomem"] = "iomem.txt"
        self.publish("sysfs-before.json", self.snapshot(region, dp.name, blocks))
        self.raw_files["sysfs_before"] = "sysfs-before.json"
        verify_ownership(iomem, dp.name, start, size)
        for block in blocks:
            if int(one(block["node_links"], "CXL block node backlink")[4:]) != slow:
                raise ValueError("CXL blocks span unexpected NUMA nodes")
            movable_state(block)
        for block in blocks:
            path = SYS / "devices/system/memory" / block["name"]
            if block["state"] == "offline":
                (path / "state").write_text("online_movable\n")
            if read(path / "state") != "online" or read(path / "valid_zones").split() != ["Movable"]:
                raise ValueError("Memory block did not become online Movable")
        self.listing("dax_devices", "dax-devices.json", ["daxctl", "list", "-D"])
        blocks = contained_blocks(start, size, self.memory_blocks())
        snap = self.snapshot(region, dp.name, blocks)
        self.publish("sysfs-after.json", snap)
        self.raw_files["sysfs_after"] = "sysfs-after.json"
        fast = fast_node(snap["nodes"], self.memory_blocks(), slow, start, size)
        return {"fast_node": fast, "slow_node": slow, "memdev": mem["memdev"],
                "memdev_volatile_bytes": mem["ram_size"], "region": region, "daxdev": dp.name,
                "root_decoder": decoder, "region_type": "ram", "interleave_ways": 1,
                "resource_start": start, "resource_size": size, "blocks": blocks,
                "memory_tiers": snap["memory_tiers"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", args.run_id):
        parser.error("Invalid run ID")
    args.output.mkdir(parents=True, exist_ok=True)
    setup = Setup(args.output, args.run_id)
    report = {"schema_version": 1, "run_id": args.run_id, "scenario": "migrate",
              "status": "BLOCKED", "evidence_basis": "guest_kernel_measured"}
    try:
        report.update(setup.execute(), status="PASS")
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        report["reason"] = str(exc)
        print(f"BLOCKED: {exc}", file=sys.stderr)
    report["raw_files"] = setup.raw_files
    path = args.output / "topology.json"
    path.write_text(json.dumps(report, indent=2) + "\n")
    print(f"CXL topology: {report['status']}")
    return 0 if report["status"] == "PASS" else 78


if __name__ == "__main__":
    raise SystemExit(main())
