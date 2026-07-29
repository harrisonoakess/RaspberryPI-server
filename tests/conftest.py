import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# `server/main.py` and `pi/connectivity_daemon.py` are deployed as independent
# top-level modules, so tests import them the same way their runtimes do.
for package_dir in (ROOT / "server", ROOT / "pi"):
    if str(package_dir) not in sys.path:
        sys.path.insert(0, str(package_dir))
