"""Loading the shipped config/reviewer.json on a machine without the App key.

The shipped config posts as the GitHub App, and ``load_config`` refuses a
``github_app.private_key_path`` that is not a file. The key exists only on the
production host, so tests that read the shipped config swap that one path for
a throwaway file. Every other ``github_app`` check still runs, and production
validation is unchanged.
"""

from __future__ import annotations

import contextlib
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterator
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from code_review_service import service as svc

SHIPPED_CONFIG_PATH = ROOT / "config" / "reviewer.json"


@contextlib.contextmanager
def app_key_stand_in() -> Iterator[Path]:
    """While active, ``github_app.private_key_path`` names a temp file.

    Patched on the service module, so loaders that import it themselves
    (``run_doctor``, install-dashboard's ``executor_paths``) see it too.
    Nothing signs with the file: it only has to exist.
    """
    real = svc.validate_github_app
    with tempfile.TemporaryDirectory() as tmp:
        key = Path(tmp) / "github-app.pem"
        key.write_text("not a key: only its existence is checked\n", encoding="utf-8")

        def validate(raw: Any, config_dir: Path) -> Dict[str, Any]:
            if isinstance(raw, dict):
                raw = {**raw, "private_key_path": str(key)}
            return real(raw, config_dir)

        with mock.patch.object(svc, "validate_github_app", new=validate):
            yield key


def load_shipped_config() -> Dict[str, Any]:
    with app_key_stand_in():
        return svc.load_config(SHIPPED_CONFIG_PATH)
