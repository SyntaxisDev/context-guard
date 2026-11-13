"""Shared installation contracts, composed only by the scenario suite."""

import json

from tests import harness


class InstallationContracts:
    """Runtime-neutral installation assertions using the isolated scenario driver."""

    def test_handoff_access(self) -> None:
        self.assertEqual(self.driver.install().returncode, 0)
        self.driver.configure_access()
        root = str(self.home / ".context-guard" / "handoffs")
        self.assertEqual(self.driver.access_grants(), {root})

    def test_roles_installed(self) -> None:
        self.assertEqual(self.driver.install().returncode, 0)
        paths = self.driver.installed_roles()
        self.assertTrue(paths)
        for name in ("scout", "reviewer", "runner", "implementer", "investigator"):
            self.assertTrue(any(name in str(path) for path in paths), name)

    def test_roles_models(self) -> None:
        self.assertEqual(self.driver.install().returncode, 0)
        roles = json.loads((harness.ROOT / "content/roles/roles.json").read_text())["roles"]
        tiers = json.loads((harness.ROOT / "adapters" / self.adapter.name / "config.json").read_text())["tiers"]
        expected = {
            name: tiers[role["default_tier"]].get("model", tiers[role["default_tier"]].get("call"))
            for name, role in roles.items()
        }
        self.assertEqual(self.driver.role_models(), expected)

    def test_runner_pinned_classifier_calibration(self) -> None:
        evidence = self.driver.runner_contract()
        for key in ("model_pinned", "routed", "confirmed", "executed", "invalid_command_rejected"):
            self.assertIs(type(evidence[key]), bool)
            self.assertTrue(evidence[key], key)

    def test_install_uninstall_preservation(self) -> None:
        originals = self.driver.seed_user_files()
        self.assertEqual(self.driver.install().returncode, 0)
        self.assertTrue(self.driver.registered_commands())
        self.assertEqual(self.driver.uninstall().returncode, 0)
        for path, content in originals.items():
            self.assertEqual(path.read_bytes(), content)

    def test_install_dry_run(self) -> None:
        self.driver.seed_user_files()
        before = {path.relative_to(self.home): path.read_bytes() for path in self.home.rglob("*") if path.is_file()}
        self.assertEqual(self.driver.dry_run().returncode, 0)
        after = {path.relative_to(self.home): path.read_bytes() for path in self.home.rglob("*") if path.is_file()}
        self.assertEqual(before, after)

    def test_doctor_health_and_drift(self) -> None:
        self.assertEqual(self.driver.install().returncode, 0)
        self.assertEqual(self.driver.doctor().returncode, 0)
        self.driver.introduce_drift()
        self.assertNotEqual(self.driver.doctor().returncode, 0)

    def test_skills_installed(self) -> None:
        self.assertEqual(self.driver.install().returncode, 0)
        paths = self.driver.installed_skills()
        self.assertTrue(paths)
        self.assertTrue(any("handoff" in str(path) for path in paths))
        self.assertTrue(all(path.is_file() for path in paths))
