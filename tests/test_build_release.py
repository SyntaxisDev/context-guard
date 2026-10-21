"""scripts/build_release.py: plugin and marketplace zips per runtime, checksums and the tag check."""

import hashlib
import io
import json
import tempfile
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path, PurePosixPath

import cli
from adapters.claude import plugin
from core import package
from scripts import build_release


def package_claude(out: Path, *extra: str) -> int:
    """Package the Claude plugin into an output tree.

    Args:
        out: The `--out` directory.
        *extra: Further package arguments.

    Returns:
        The CLI exit code.
    """
    return cli.main(["package", "--runtime", "claude", "--out", str(out), *extra], out=io.StringIO())


class BuildReleaseTests(unittest.TestCase):
    """A Claude package with a marketplace listing rendered into a temporary output tree, then zipped."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name) / "dist"
        self.assertEqual(package_claude(self.out, "--marketplace", "context-guard"), cli.EXIT_OK)
        self.version = build_release.project_version(build_release.ROOT)
        self.plugin_zip = self.out / f"{plugin.NAME}-claude-{self.version}.zip"
        self.market_zip = self.out / f"{plugin.NAME}-claude-marketplace-{self.version}.zip"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_script(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = build_release.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_plugin_zip_root_is_the_plugin_root_with_its_manifest(self) -> None:
        code, said, _ = self.run_script("--out", str(self.out))
        self.assertEqual(code, 0)
        sums = self.out / build_release.CHECKSUMS
        self.assertEqual(said.splitlines(), [str(self.plugin_zip), str(self.market_zip), str(sums)])
        root = self.out / "claude" / package.PLUGINS_DIR / plugin.NAME
        with zipfile.ZipFile(self.plugin_zip) as opened:
            names = opened.namelist()
            launcher = opened.getinfo(package.LAUNCHER)
        self.assertIn(plugin.MANIFEST_FILE, names)
        self.assertEqual(sorted(names), sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()))
        self.assertEqual(launcher.external_attr >> 16 & 0o777, 0o755)

    def test_marketplace_zip_lists_the_plugin_it_contains(self) -> None:
        self.run_script("--out", str(self.out))
        with zipfile.ZipFile(self.market_zip) as opened:
            names = set(opened.namelist())
            listing = json.loads(opened.read(plugin.MARKETPLACE_FILE))
        [entry] = listing["plugins"]
        self.assertEqual(listing["name"], "context-guard")
        source = PurePosixPath(entry["source"]).relative_to(".")
        self.assertIn((source / plugin.MANIFEST_FILE).as_posix(), names)

    def test_rebuild_is_byte_identical_and_checksums_cover_every_zip(self) -> None:
        self.run_script("--out", str(self.out))
        first = [self.plugin_zip.read_bytes(), self.market_zip.read_bytes()]
        self.run_script("--out", str(self.out))
        self.assertEqual([self.plugin_zip.read_bytes(), self.market_zip.read_bytes()], first)
        expected = "".join(
            f"{hashlib.sha256(data).hexdigest()}  {path.name}\n"
            for path, data in zip((self.plugin_zip, self.market_zip), first, strict=True)
        )
        self.assertEqual((self.out / build_release.CHECKSUMS).read_text(), expected)

    def test_tag_must_match_the_project_version(self) -> None:
        self.assertEqual(self.run_script("--tag", f"v{self.version}")[0], 0)
        code, _, err = self.run_script("--tag", "v0.0.0-mismatch", "--out", str(self.out))
        self.assertEqual(code, 1)
        self.assertIn("does not match project.version", err)
        self.assertFalse(list(self.out.glob("*.zip")))

    def test_tree_without_plugins_fails(self) -> None:
        code, _, err = self.run_script("--out", str(Path(self.tmp.name) / "empty"))
        self.assertEqual(code, 1)
        self.assertIn("holds no", err)

    def test_tree_without_marketplace_listing_fails_before_writing(self) -> None:
        bare = Path(self.tmp.name) / "bare"
        self.assertEqual(package_claude(bare), cli.EXIT_OK)
        code, _, err = self.run_script("--out", str(bare))
        self.assertEqual(code, 1)
        self.assertIn("--marketplace", err)
        self.assertFalse(list(bare.glob("*.zip")))


if __name__ == "__main__":
    unittest.main()
