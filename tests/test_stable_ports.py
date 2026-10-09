"""Persistent allocation tests; never launch demo or Cloud processes."""
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]

class StablePorts(unittest.TestCase):
    def test_runtime_cleanup_preserves_assignment_and_collision_refuses(self):
        scratch = ROOT / ".runtime" / "port-tests"
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as temporary:
            home = Path(temporary)
            demo = home / "demo-fixture"
            demo.mkdir()
            (demo / "ports.json").write_text(json.dumps({
                "version": 2, "demo": "demo-fixture",
                "ports": {"PORT": None, "EDGE_API_PORT": None}}))
            env = dict(os.environ, DEMO_PORT_STATE=str(home / "state.json"))
            def resolve(*options):
                return subprocess.run(["uv", "run", "--no-project",
                    str(ROOT / "scripts/demo-ports.py"), "resolve",
                    "--demo-dir", str(demo), *options], env=env,
                    capture_output=True, text=True)
            first = resolve()
            self.assertEqual(first.returncode, 0, first.stderr)
            ports = json.loads(first.stdout)
            self.assertEqual(len(set(ports.values())), 2)
            runtime = demo / ".runtime"
            runtime.mkdir()
            shutil.rmtree(runtime)
            again = resolve()
            self.assertEqual(again.returncode, 0, again.stderr)
            self.assertEqual(ports, json.loads(again.stdout))
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", ports["PORT"]))
                listener.listen()
                refused = resolve()
                self.assertNotEqual(refused.returncode, 0)
                self.assertIn("URL preserved", refused.stderr)
                status = resolve("--allow-bound")
                self.assertEqual(status.returncode, 0, status.stderr)
                self.assertEqual(ports, json.loads(status.stdout))
            restored = resolve()
            self.assertEqual(restored.returncode, 0, restored.stderr)
            self.assertEqual(ports, json.loads(restored.stdout))

if __name__ == "__main__":
    unittest.main()
