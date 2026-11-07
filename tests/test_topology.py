"""Pure CXL topology checks; these tests never access memory sysfs."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location(
    "cxl_setup", Path(__file__).resolve().parents[1] / "guest/cxl_setup.py")
topology = importlib.util.module_from_spec(spec)
spec.loader.exec_module(topology)


class TopologyTests(unittest.TestCase):
    def test_nested_json_discovery_and_ambiguous_node(self):
        payload = [{"bus": "root3", "children": [{"memdev": "mem9"}]}]
        self.assertEqual(topology.one((o["memdev"] for o in topology.objects(payload)
                                       if "memdev" in o), "memdev"), "mem9")
        for links in ([], ["node2", "node7"]):
            with self.assertRaisesRegex(ValueError, "Expected one"):
                topology.one(links, "memory node backlink")

    def test_physical_ranges_reject_overflow_and_invalid_types(self):
        self.assertEqual(topology.interval((1 << 64) - 1, 1), ((1 << 64) - 1, 1 << 64))
        for start, size in ((-1, 1), (1 << 64, 1), ((1 << 64) - 1, 2),
                            (0, 0), (0, -1), (True, 1), (0, False), ("0", 1)):
            with self.subTest(start=start, size=size), self.assertRaises(ValueError):
                topology.interval(start, size)

    def test_complete_blocks_are_sorted_and_exclude_touching_neighbors(self):
        blocks = [{"start": p, "size": 16} for p in (48, 32, 0, 16)]
        selected = topology.contained_blocks(16, 32, blocks)
        self.assertEqual([b["start"] for b in selected], [16, 32])

    def test_partial_blocks_holes_and_duplicate_membership_are_rejected(self):
        for blocks in ([{"start": 8, "size": 16}],
                       [{"start": 16, "size": 8}],
                       [{"start": 16, "size": 16}, {"start": 24, "size": 8}],
                       [{"start": 16, "size": 16}, {"start": 16, "size": 16}], []):
            with self.subTest(blocks=blocks), self.assertRaises(ValueError):
                topology.contained_blocks(16, 16, blocks)

    def test_only_movable_blocks_can_be_reused(self):
        topology.movable_state({"state": "offline", "valid_zones": "Normal Movable"})
        topology.movable_state({"state": "online", "valid_zones": "Movable"})
        for state, zones in (("online", "Normal"), ("online", "DMA32"),
                             ("online", "Movable Normal"), ("offline", "Normal"),
                             ("going-offline", "Movable"), ("online_kernel", "Normal")):
            with self.subTest(state=state, zones=zones), self.assertRaises(ValueError):
                topology.movable_state({"state": state, "valid_zones": zones})

    def test_iomem_requires_matching_dax_owner_and_full_kmem_coverage(self):
        good = "1000-1fff : dax5.7\n  1000-17ff : System RAM (kmem)\n  1800-1fff : System RAM (kmem)"
        topology.verify_ownership(good, "dax5.7", 4096, 4096)
        for bad in (good.replace("dax5.7", "dax0.0"),
                    good.replace("1800-1fff", "1900-1fff"),
                    "0fff-1fff : dax5.7\n  1000-1fff : System RAM (kmem)",
                    "1000-1fff : dax5.7\n  1000-1fff : System RAM"):
            with self.subTest(iomem=bad), self.assertRaises(ValueError):
                topology.verify_ownership(bad, "dax5.7", 4096, 4096)

    def test_fast_node_uses_cpu_and_ordinary_ram_outside_cxl(self):
        nodes = [{"node": 2, "cpulist": "0-3"}, {"node": 7, "cpulist": ""}]
        blocks = [{"node": 2, "state": "online", "valid_zones": "DMA32",
                   "start": 0, "size": 4096}]
        self.assertEqual(topology.fast_node(nodes, blocks, 7, 8192, 4096), 2)
        for invalid in ([dict(blocks[0], start=8192)], [dict(blocks[0], valid_zones="Movable")],
                        [dict(blocks[0], node=7)]):
            with self.subTest(blocks=invalid), self.assertRaises(ValueError):
                topology.fast_node(nodes, invalid, 7, 8192, 4096)
        with self.assertRaises(ValueError):
            topology.fast_node([dict(nodes[0], cpulist="")], blocks, 7, 8192, 4096)
        with self.assertRaises(ValueError):
            topology.fast_node(nodes + [{"node": 3, "cpulist": "4"}],
                               blocks + [dict(blocks[0], node=3)], 7, 8192, 4096)

    def test_host_kernel_guard_precedes_commands_and_sysfs_writes(self):
        with tempfile.TemporaryDirectory() as temp:
            setup = topology.Setup(Path(temp), "test-host-guard")
            with mock.patch.object(topology.platform, "release", return_value="6.6.0-microsoft-standard-WSL2"), \
                 mock.patch.object(topology.Path, "write_text") as writes, \
                 mock.patch.object(setup, "command") as command:
                with self.assertRaisesRegex(ValueError, "pinned Guest"):
                    setup.execute()
                writes.assert_not_called()
                command.assert_not_called()

    def test_kernel_version_alone_cannot_authorize_guest_writes(self):
        with mock.patch.object(topology.platform, "release", return_value="6.18.56"), \
             mock.patch.object(topology, "read", side_effect=["Microsoft Corporation", "Virtual Machine"]):
            with self.assertRaisesRegex(ValueError, "outside QEMU"):
                topology.guest_identity()


if __name__ == "__main__":
    unittest.main()
