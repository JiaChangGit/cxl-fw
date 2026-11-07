"""Focused failure controls for pinned downloads, extraction and Kconfig."""
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import build
from verify_kernel_config import verify


class BuildBoundaryTests(unittest.TestCase):
    def test_unverified_pin_is_rejected_before_io(self):
        with self.assertRaisesRegex(ValueError, "verified source_sha256"):
            build.validated_entry({"qemu": {"version": "11.1.0", "source_sha256": None}}, "qemu")

    def test_version_and_transport_cannot_silently_change(self):
        item = {"version": "11.1.1", "source_sha256": "a" * 64,
                "source_url": "https://download.qemu.org/qemu-11.1.1.tar.xz"}
        with self.assertRaisesRegex(ValueError, "Unexpected qemu version"):
            build.validated_entry({"qemu": item}, "qemu")
        item.update(version="11.1.0", source_url="http://example.org/qemu-11.1.0.tar.xz")
        with self.assertRaisesRegex(ValueError, "HTTPS source_url"):
            build.validated_entry({"qemu": item}, "qemu")

    def test_corrupt_cached_archive_is_not_trusted(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "qemu-11.1.0.tar.xz").write_bytes(b"corrupted")
            item = {"filename": "qemu-11.1.0.tar.xz", "source_sha256": "a" * 64}
            with self.assertRaisesRegex(ValueError, "Cached source checksum mismatch"):
                build.download(item, root)

    def test_archive_traversal_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archive = root / "source.tar"
            with tarfile.open(archive, "w") as bundle:
                member = tarfile.TarInfo("qemu-11.1.0/../escaped")
                member.size = 1
                bundle.addfile(member, io.BytesIO(b"x"))
            with self.assertRaisesRegex(ValueError, "outside expected source root"):
                build.extract(archive, root / "sources", "qemu-11.1.0", "a" * 64)
            self.assertFalse((root / "sources/escaped").exists())

    def test_only_exact_unused_edk2_absolute_link_is_skipped(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archive = root / "source.tar"
            link_name = "qemu-11.1.0/roms/edk2/EmulatorPkg/Unix/Host/X11IncludeHack"
            with tarfile.open(archive, "w") as bundle:
                normal = tarfile.TarInfo("qemu-11.1.0/VERSION")
                normal.size = 1
                bundle.addfile(normal, io.BytesIO(b"x"))
                link = tarfile.TarInfo(link_name)
                link.type = tarfile.SYMTYPE
                link.linkname = "/opt/X11/include"
                bundle.addfile(link)
            source = build.extract(archive, root / "sources", "qemu-11.1.0", "a" * 64)
            self.assertEqual((source / "VERSION").read_bytes(), b"x")
            self.assertFalse((root / "sources" / link_name).exists())
            stamp = json.loads((root / "sources/qemu-11.1.0.source.json").read_text())
            self.assertEqual(stamp["excluded_members"][0]["link_target"], "/opt/X11/include")

    def test_other_absolute_archive_links_still_fail(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archive = root / "source.tar"
            with tarfile.open(archive, "w") as bundle:
                link = tarfile.TarInfo("qemu-11.1.0/not-allowed")
                link.type = tarfile.SYMTYPE
                link.linkname = "/etc"
                bundle.addfile(link)
            with self.assertRaises(tarfile.AbsoluteLinkError):
                build.extract(archive, root / "sources", "qemu-11.1.0", "a" * 64)

    def test_cached_source_needs_matching_provenance(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "qemu-11.1.0").mkdir()
            with self.assertRaisesRegex(ValueError, "extraction provenance"):
                build.extract(root / "not-read.tar", root, "qemu-11.1.0", "a" * 64)

    def test_kconfig_dependency_downgrade_is_not_pass(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fragment, config = root / "fragment", root / ".config"
            fragment.write_text("CONFIG_CXL_BUS=y\n")
            config.write_text("CONFIG_CXL_BUS=m\n")
            with self.assertRaisesRegex(ValueError, "expected y, got m"):
                verify(config, fragment)

    def test_unknown_disabled_symbol_is_not_pass(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "Kconfig").write_text("config CXL_BUS\n\tbool\n")
            fragment, config = root / "fragment", root / ".config"
            fragment.write_text("# CONFIG_MEMORY_TIERING is not set\n")
            config.write_text("")
            with self.assertRaisesRegex(ValueError, "Unknown symbol"):
                verify(config, fragment, root)


if __name__ == "__main__":
    unittest.main()
