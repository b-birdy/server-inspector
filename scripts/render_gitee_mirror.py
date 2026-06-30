#!/usr/bin/env python3
"""Render Gitee-specific mirror files from the GitHub-primary workspace.

Usage:
    python scripts/render_gitee_mirror.py

This script mutates README.md and install.sh in-place so they become suitable
for publishing to the Gitee mirror, where install examples and default download
sources should point at Gitee instead of GitHub.
"""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
INSTALL = ROOT / "install.sh"


def replace_many(text: str, pairs):
    for old, new in pairs:
        text = text.replace(old, new)
    return text


def render_readme() -> None:
    text = README.read_text(encoding="utf-8")
    text = replace_many(
        text,
        [
            (
                "[![Platform: Linux](https://img.shields.io/badge/Platform-Linux-green.svg)](https://github.com/b-birdy/server-inspector)",
                "[![Platform: Linux](https://img.shields.io/badge/Platform-Linux-green.svg)](https://gitee.com/wzxdcyy/server-inspector)",
            ),
            (
                "curl -fsSL https://raw.githubusercontent.com/b-birdy/server-inspector/master/install.sh | bash",
                "curl -fsSL https://gitee.com/wzxdcyy/server-inspector/raw/master/install.sh | bash",
            ),
            (
                "curl -fsSL https://raw.githubusercontent.com/b-birdy/server-inspector/master/install.sh | bash -s -- --uninstall",
                "curl -fsSL https://gitee.com/wzxdcyy/server-inspector/raw/master/install.sh | bash -s -- --uninstall",
            ),
            (
                "curl -fsSL https://raw.githubusercontent.com/b-birdy/server-inspector/master/install.sh | bash -s -- --version v1.2.0",
                "curl -fsSL https://gitee.com/wzxdcyy/server-inspector/raw/master/install.sh | bash -s -- --version v1.2.0",
            ),
            (
                "curl -fsSL https://raw.githubusercontent.com/b-birdy/server-inspector/master/install.sh | bash -s -- --dir /opt/server-inspector",
                "curl -fsSL https://gitee.com/wzxdcyy/server-inspector/raw/master/install.sh | bash -s -- --dir /opt/server-inspector",
            ),
            (
                "curl -fsSL https://gitee.com/wzxdcyy/server-inspector/raw/master/install.sh | REPO_HOST=gitee.com REPO=wzxdcyy/server-inspector bash",
                "curl -fsSL https://gitee.com/wzxdcyy/server-inspector/raw/master/install.sh | bash",
            ),
            (
                "curl -fsSL https://gitee.com/wzxdcyy/server-inspector/raw/master/install.sh | REPO_HOST=gitee.com REPO=wzxdcyy/server-inspector bash -s -- --version v1.2.0",
                "curl -fsSL https://gitee.com/wzxdcyy/server-inspector/raw/master/install.sh | bash -s -- --version v1.2.0",
            ),
        ],
    )
    README.write_text(text, encoding="utf-8", newline="\n")


def render_install() -> None:
    text = INSTALL.read_text(encoding="utf-8")
    text = replace_many(
        text,
        [
            (
                'REPO="${REPO:-b-birdy/server-inspector}"',
                'REPO="${REPO:-wzxdcyy/server-inspector}"',
            ),
            (
                'REPO_HOST="${REPO_HOST:-github.com}"',
                'REPO_HOST="${REPO_HOST:-gitee.com}"',
            ),
            (
                "REPO_HOST               Download host (default: github.com, or gitee.com)",
                "REPO_HOST               Download host (default: gitee.com, or github.com)",
            ),
        ],
    )
    INSTALL.write_text(text, encoding="utf-8", newline="\n")


def main() -> None:
    render_readme()
    render_install()
    print("Rendered Gitee mirror files: README.md, install.sh")


if __name__ == "__main__":
    main()
