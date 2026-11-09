#!/usr/bin/env python3
"""Build a deterministic runtime archive and formula; never publish either."""
import argparse
import gzip
import hashlib
import importlib.util
import io
from pathlib import Path
import re
import subprocess
import tarfile

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ("code_review_service", "config/reviewer.json", "agents", "bin/rol", "LICENSE", "THIRD_PARTY_NOTICES.md", "README.md", "SECURITY.md")


def build(version: str, out: Path, root: Path = ROOT, tag: "str | None" = None) -> "tuple[Path, Path]":
    """Archive + SHA256SUMS + formula for ``tag`` (default ``v<version>``).

    Stable: version MAJOR.MINOR.PATCH, tag v<version>. Preview: version
    YYYY.MM.DD.HHMM, tag preview-<YYYY-MM-DD-HHMM>-<sha12>. VERSION in the
    archive holds the tag, which names the dashboard venv on a host.
    """
    tag = tag or "v" + version
    if not re.fullmatch(r"\d+\.\d+\.\d+(\.\d+)?", version):
        raise ValueError("version must be MAJOR.MINOR.PATCH (or YYYY.MM.DD.HHMM for a preview)")
    prefix = "review-of-legends-" + version
    out.mkdir(parents=True, exist_ok=True)
    archive = out / (prefix + ".tar.gz")
    with archive.open("wb") as target, gzip.GzipFile(filename="", mode="wb", fileobj=target, mtime=0) as zipped:
        with tarfile.open(fileobj=zipped, mode="w") as tar:
            # Export only tracked runtime inputs, never untracked local credentials.
            tracked = subprocess.check_output(["git", "ls-files", "-z", "--", *RUNTIME], cwd=root)
            sources = [root / name.decode() for name in tracked.split(b"\0") if name]
            for path in sorted(sources):
                if path.is_symlink():
                    raise ValueError(f"release input must not be a symlink: {path.relative_to(root)}")
                data = path.read_bytes()
                info = tarfile.TarInfo(prefix + "/" + path.relative_to(root).as_posix())
                info.size = len(data)
                info.mode = 0o755 if path.relative_to(root).as_posix() == "bin/rol" else 0o644
                tar.addfile(info, io.BytesIO(data))
            data = (tag + "\n").encode()
            info = tarfile.TarInfo(prefix + "/VERSION")
            info.size = len(data)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(data))
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    (out / "SHA256SUMS").write_text(digest + "  " + archive.name + "\n")
    spec = importlib.util.spec_from_file_location("rol_formula", root / "scripts/homebrew-formula.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    formula = out / ("review-of-legends-preview.rb" if tag.startswith("preview-") else "review-of-legends.rb")
    formula.write_text(module.render(version, digest, tag))
    return archive, formula


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--tag", help="release tag (default v<version>)")
    args = parser.parse_args()
    try:
        archive, formula = build(args.version, args.out, tag=args.tag)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(archive)
    print(formula)
