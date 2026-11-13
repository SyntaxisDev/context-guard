"""Independent project CLI permission operations for the Cursor conformance driver."""

import json
import subprocess
import sys
from pathlib import Path

from tests import harness


class AccessOperations:
    """Compose public access setup and native grant inspection into a Cursor driver."""

    home: Path
    project: Path

    def configure_access(self) -> None:
        """Run the public helper against only the driver's temporary project and home.

        Raises:
            AssertionError: The public setup command fails.
        """
        (self.home / ".context-guard/handoffs").mkdir(parents=True, exist_ok=True)
        result = subprocess.run(  # noqa: S603
            [
                sys.executable,
                str(harness.ROOT / "adapters/cursor/access.py"),
                "install",
                "--home",
                str(self.home),
                "--workspace",
                str(self.project),
            ],
            env=harness.hook_env(self.home),
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode:
            raise AssertionError(result)

    def access_grants(self) -> set[str]:
        """Read exact Write subtree grants directly from the native project JSON.

        Returns:
            Directory paths granted by native Write subtree entries.
        """
        data = json.loads((self.project.resolve() / ".cursor/cli.json").read_text())
        return {
            rule[6:-4]
            for rule in data.get("permissions", {}).get("allow", [])
            if rule.startswith("Write(") and rule.endswith("/**)")
        }
