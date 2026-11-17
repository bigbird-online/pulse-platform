"""Fast startup checks for the real-source acceptance entrypoint; no source reads."""
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "uat/current_source_acceptance.py"


class AcceptanceEntrypointTests(unittest.TestCase):
    def isolated(self, args):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            home.mkdir()
            env = {"PATH": os.environ.get("PATH", ""), "HOME": str(home),
                   "PYTHONDONTWRITEBYTECODE": "1"}
            # The selected CI CPython may use a shared libpython. Preserve only
            # its actual relocated prefix, not sysconfig's build-time LIBDIR.
            library_dir = Path(sys.base_prefix) / "lib"
            if sys.platform.startswith("linux") and library_dir.is_dir():
                env["LD_LIBRARY_PATH"] = str(library_dir)
            result = subprocess.run([sys.executable, *args], cwd=directory, env=env,
                                    capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(list(home.iterdir()), [], "startup must not read/write production state")
            return result.stdout

    def test_help_from_unrelated_directory_needs_no_production_configuration(self):
        output = self.isolated([str(SCRIPT), "--help"])
        self.assertIn("--archive", output)
        self.assertIn("--sha", output)

    def test_actual_exporter_is_resolved_before_any_expensive_replay(self):
        code = ("import runpy,sys; from pathlib import Path; "
                "script=Path(sys.argv[1]); sys.path.insert(0,str(script.parent)); "
                "scope=runpy.run_path(str(script)); "
                "loader=scope['load_cost_dashboard']; "
                "assert callable(loader) and loader.__module__=='pulse_stats.cost_dashboard'")
        self.isolated(["-c", code, str(SCRIPT)])


if __name__ == "__main__":
    unittest.main()
