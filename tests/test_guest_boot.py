"""Guest identity failures must never power off a Host."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "guest/run.sh"


class GuestIdentityTests(unittest.TestCase):
    def test_host_identity_rejection_has_no_shutdown_side_effect(self):
        for kernel in ("6.18.33.2-microsoft-standard-WSL2", "6.18.56", "6.17.0"):
            with self.subTest(kernel=kernel), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                commands = {
                    "uname": 'printf "%s\\n" "$FAKE_KERNEL"\n',
                    "cat": 'case "$1" in\n*/boot_id) echo 12345678-1234-1234-1234-123456789012;;\n*/sys_vendor) echo LaptopVendor;;\n*/product_name) echo Laptop;;\n*) exec /bin/cat "$@";;\nesac\n',
                    "systemctl": ': > "$SHUTDOWN_MARKER"\n',
                    "sync": 'exit 0\n',
                }
                for name, content in commands.items():
                    path = root / name
                    path.write_text("#!/bin/bash\n" + content)
                    path.chmod(0o755)
                marker = root / "shutdown-called"
                env = dict(os.environ, PATH=str(root)+":"+os.environ["PATH"],
                           FAKE_KERNEL=kernel, SHUTDOWN_MARKER=str(marker))
                result = subprocess.run(["bash", str(SCRIPT)], env=env,
                                        capture_output=True, text=True, timeout=5)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(marker.exists(), "Host identity failure called systemctl")
                self.assertIn("ERROR:", result.stderr)


if __name__ == "__main__":
    unittest.main()
