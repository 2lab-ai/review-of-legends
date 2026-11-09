#!/usr/bin/env python3
"""Render the tap formula for an immutable, checksummed source release."""
import argparse
import re


STABLE_TAG = re.compile(r"v\d+\.\d+\.\d+")
PREVIEW_TAG = re.compile(r"preview-\d{4}-\d{2}-\d{2}-\d{4}-[0-9a-f]{12}")


def render(version, sha256, tag=None):
    """The formula for one release. ``tag`` defaults to ``v<version>`` (stable).

    A ``preview-<YYYY-MM-DD-HHMM>-<sha12>`` tag renders the separate
    ``review-of-legends-preview`` formula (version ``YYYY.MM.DD.HHMM``), as
    llmux's preview channel does.
    """
    tag = tag or "v" + version
    preview = PREVIEW_TAG.fullmatch(tag) is not None
    if preview:
        if not re.fullmatch(r"\d{4}\.\d{2}\.\d{2}\.\d{4}", version):
            raise ValueError("a preview version must be YYYY.MM.DD.HHMM")
    elif not (STABLE_TAG.fullmatch(tag) and tag == "v" + version):
        raise ValueError("version must be MAJOR.MINOR.PATCH and the tag v<version>")
    if not re.fullmatch(r"[a-f0-9]{64}", sha256):
        raise ValueError("sha256 must be 64 lowercase hexadecimal characters")
    head = '''class %s < Formula
  desc "Self-hosted multi-agent GitHub pull request reviews"
  homepage "https://github.com/2lab-ai/review-of-legends"
  url "https://github.com/2lab-ai/review-of-legends/releases/download/%s/review-of-legends-%s.tar.gz"
%s  sha256 "%s"
  license "MIT"
''' % (
        "ReviewOfLegendsPreview" if preview else "ReviewOfLegends",
        tag,
        version,
        # Homebrew reads a stable version from the URL; a preview tag does not parse.
        '  version "%s"\n' % version if preview else "",
        sha256,
    )
    if preview:
        head += '''
  conflicts_with "review-of-legends", because: "both install the rol command"
'''
    else:
        head += '''
  conflicts_with "review-of-legends-preview", because: "both install the rol command"
'''
    return head + BODY % ("review-of-legends-preview" if preview else "review-of-legends", tag)


BODY = '''

  depends_on "python@3.13"
  depends_on "gh"
  depends_on "git"
  depends_on "openssl@3"

  def install
    libexec.install "code_review_service", "config", "agents", "VERSION", "THIRD_PARTY_NOTICES.md"
    (bin/"rol").write <<~SH
      #!/bin/sh
      export ROL_ASSETS="#{opt_libexec}"
      export ROL_HOMEBREW=1
      export ROL_HOMEBREW_FORMULA="%s"
      export ROL_PYTHON="#{Formula["python@3.13"].opt_bin}/python3.13"
      export PYTHONPATH="#{opt_libexec}"
      export PATH="#{Formula["gh"].opt_bin}:#{Formula["git"].opt_bin}:#{Formula["openssl@3"].opt_bin}:#{HOMEBREW_PREFIX}/bin:/usr/bin:/bin:/usr/sbin:/sbin"
      exec "#{Formula["python@3.13"].opt_bin}/python3.13" -P -m code_review_service.cli "$@"
    SH
  end

  service do
    run [opt_bin/"rol", "daemon"]
    keep_alive true
    log_path var/"log/review-of-legends.log"
    error_log_path var/"log/review-of-legends.error.log"
  end

  def caveats
    "Run rol setup as your user before starting the service. Never use sudo rol setup."
  end

  test do
    assert_match "Review of Legends", shell_output("#{bin}/rol --help")
    assert_match "%s", shell_output("#{bin}/rol version")
  end
end
'''


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("version")
    parser.add_argument("sha256")
    parser.add_argument("--tag", help="release tag (default v<version>; preview-... renders the preview formula)")
    args = parser.parse_args()
    try:
        print(render(args.version, args.sha256, args.tag), end="")
    except ValueError as exc:
        parser.error(str(exc))
