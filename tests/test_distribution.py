"""Release artifacts exercise the real installed CLI, not source-only imports."""
import hashlib
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("rol_package", ROOT / "scripts/package-release.py")
package = importlib.util.module_from_spec(spec)
spec.loader.exec_module(package)


class ReleaseChannelTest(unittest.TestCase):
    """Two channels, as llmux: a stable vX.Y.Z formula and a preview-<stamp>-<sha12> formula."""

    def test_stable_formula_reads_its_version_from_the_url(self):
        formula = package_formula().render("1.2.3", "a" * 64)
        self.assertIn("class ReviewOfLegends < Formula", formula)
        self.assertIn("releases/download/v1.2.3/review-of-legends-1.2.3.tar.gz", formula)
        self.assertNotIn('  version "', formula)
        self.assertIn('conflicts_with "review-of-legends-preview"', formula)
        self.assertIn('assert_match "v1.2.3"', formula)
        self.assertIn('export ROL_HOMEBREW_FORMULA="review-of-legends"', formula)

    def test_preview_formula_is_a_separate_formula(self):
        tag = "preview-2026-11-09-0612-0123456789ab"
        formula = package_formula().render("2026.11.09.0612", "b" * 64, tag)
        self.assertIn("class ReviewOfLegendsPreview < Formula", formula)
        self.assertIn("releases/download/%s/review-of-legends-2026.11.09.0612.tar.gz" % tag, formula)
        self.assertIn('  version "2026.11.09.0612"', formula)
        self.assertIn('conflicts_with "review-of-legends",', formula)
        self.assertIn('assert_match "%s"' % tag, formula)
        # rol start/stop/status manage the preview's own service.
        self.assertIn('export ROL_HOMEBREW_FORMULA="review-of-legends-preview"', formula)

    def test_mismatched_versions_and_tags_are_refused(self):
        render = package_formula().render
        for args in (("1.2", "a" * 64), ("1.2.3", "a" * 64, "v1.2.4"), ("1.2.3", "a" * 64, "preview-2026-11-09-0612-0123456789ab"),
                     ("2026.11.09.0612", "a" * 64, "preview-latest"), ("1.2.3", "A" * 64)):
            with self.subTest(args=args):
                with self.assertRaises(ValueError):
                    render(*args)

    def test_preview_archive_records_the_tag(self):
        tag = "preview-2026-11-09-0612-0123456789ab"
        with tempfile.TemporaryDirectory() as tmp:
            archive, formula = package.build("2026.11.09.0612", Path(tmp), tag=tag)
            self.assertEqual("review-of-legends-preview.rb", formula.name)
            with tarfile.open(archive) as tar:
                version = tar.extractfile("review-of-legends-2026.11.09.0612/VERSION").read().decode()
            self.assertEqual(tag + "\n", version)


class HomebrewServiceNameTest(unittest.TestCase):
    def test_the_cli_manages_the_service_of_the_formula_it_came_from(self):
        sys.path.insert(0, str(ROOT))
        from code_review_service import cli
        from unittest import mock
        for env, expected in (({}, "review-of-legends"),
                              ({"ROL_HOMEBREW_FORMULA": "review-of-legends-preview"}, "review-of-legends-preview"),
                              ({"ROL_HOMEBREW_FORMULA": "something; rm -rf /"}, "review-of-legends")):
            with self.subTest(env=env), mock.patch.dict(os.environ, env, clear=False):
                if not env:
                    os.environ.pop("ROL_HOMEBREW_FORMULA", None)
                self.assertEqual(expected, cli.homebrew_formula())


def package_formula():
    spec = importlib.util.spec_from_file_location("rol_formula", ROOT / "scripts/homebrew-formula.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DistributionTest(unittest.TestCase):
    def test_reproducible_archive_contains_runtime_and_runs_outside_checkout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first, formula = package.build("0.0.0", root / "one")
            second, _ = package.build("0.0.0", root / "two")
            self.assertEqual(first.read_bytes(), second.read_bytes())
            digest = hashlib.sha256(first.read_bytes()).hexdigest()
            self.assertIn(digest, formula.read_text())
            self.assertIn("releases/download/v0.0.0/review-of-legends-0.0.0.tar.gz", formula.read_text())
            with tarfile.open(first) as tar:
                names = tar.getnames()
                self.assertFalse(any(".git" in p or "__pycache__" in p or p.endswith(".pem") for p in names))
                self.assertEqual([p for p in names if "/config/" in p], ["review-of-legends-0.0.0/config/reviewer.json"])
                self.assertIn("review-of-legends-0.0.0/LICENSE", names)
                self.assertIn("review-of-legends-0.0.0/THIRD_PARTY_NOTICES.md", names)
                tar.extractall(root / "installed")
            installed = root / "installed/review-of-legends-0.0.0"
            env = {**os.environ, "PYTHONPATH": str(installed), "ROL_HOME": str(root / "home")}
            for command, expected in (("version", "v0.0.0"), ("--help", "Review of Legends")):
                result = subprocess.run([sys.executable, "-m", "code_review_service.cli", command], cwd=root, env=env, text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(expected, result.stdout)
            self.assertFalse((root / "home").exists(), "help/version must not provision settings")
