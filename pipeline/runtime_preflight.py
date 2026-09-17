#!/usr/bin/env python3
"""Preflight checks for the First Light runtime.

The runtime must be outside the ecryptfs private home. Secrets intentionally stay
inside the encrypted operator home and must be readable before Discord/Gemini
work starts.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path


def _decode_mount_field(value: str) -> str:
    return value.replace("\\040", " ").replace("\\011", "\t").replace("\\012", "\n").replace("\\134", "\\")


def _mount_entries(mounts_text: str) -> list[tuple[str, str]]:
    entries: list[tuple[str, str]] = []
    for line in mounts_text.splitlines():
        parts = line.split()
        if len(parts) >= 3:
            entries.append((_decode_mount_field(parts[1]), parts[2]))
    return entries


def _path_is_under(path: Path, mount_point: Path) -> bool:
    path_s = str(path)
    mount_s = str(mount_point)
    return path_s == mount_s or path_s.startswith(mount_s.rstrip("/") + "/")


def find_mount_for_path(path: Path, mounts_text: str | None = None) -> tuple[str, str] | None:
    mounts_text = Path("/proc/mounts").read_text(encoding="utf-8") if mounts_text is None else mounts_text
    target = Path(os.path.abspath(str(path)))
    best: tuple[str, str] | None = None
    for mount_point, fstype in _mount_entries(mounts_text):
        mp = Path(mount_point)
        if _path_is_under(target, mp) and (best is None or len(mount_point) > len(best[0])):
            best = (mount_point, fstype)
    return best


def path_uses_ecryptfs(path: Path, mounts_text: str | None = None) -> bool:
    mount = find_mount_for_path(path, mounts_text)
    return bool(mount and mount[1] in {"ecryptfs", "ecrypt"})


def validate_secret_config(
    path: Path,
    require_discord_login: bool = False,
) -> None:
    if not path.exists() or not path.is_file() or not os.access(path, os.R_OK):
        raise SystemExit(f"encrypted secret config unavailable: {path}; login required")
    text = path.read_text(encoding="utf-8", errors="replace")
    if require_discord_login:
        if not re.search(r"^DISCORD_EMAIL=\S+", text, re.M):
            raise SystemExit(f"DISCORD_EMAIL missing in encrypted secret config: {path}")
        if not re.search(r"^DISCORD_PASSWORD=\S+", text, re.M):
            raise SystemExit(f"DISCORD_PASSWORD missing in encrypted secret config: {path}")


def validate_gemini_keys_config(path: Path) -> None:
    if not path.exists() or not path.is_file() or not os.access(path, os.R_OK):
        raise SystemExit(f"encrypted Gemini key config unavailable: {path}; login required")
    text = path.read_text(encoding="utf-8", errors="replace")
    if not re.search(r"^GEMINI_API_KEYS=\S+", text, re.M):
        raise SystemExit(f"GEMINI_API_KEYS missing in encrypted key config: {path}")


def validate_certifi_bundle() -> None:
    import certifi

    bundle = Path(certifi.where())
    if not bundle.exists() or not bundle.is_file() or not os.access(bundle, os.R_OK):
        raise SystemExit(f"runtime certifi bundle unavailable: {bundle}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate First Light runtime before scheduled work")
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--secret-config", type=Path)
    parser.add_argument("--require-discord-login", action="store_true")
    parser.add_argument("--gemini-keys-config", type=Path)
    parser.add_argument("--allow-ecryptfs-runtime", action="store_true")
    args = parser.parse_args(argv)

    if path_uses_ecryptfs(args.runtime) and not args.allow_ecryptfs_runtime:
        raise SystemExit(f"runtime path is on ecryptfs, move automation outside encrypted home: {args.runtime}")

    validate_certifi_bundle()
    if args.secret_config or args.require_discord_login:
        if args.secret_config is None:
            raise SystemExit("--secret-config is required when secret validation is requested")
        validate_secret_config(
            args.secret_config,
            require_discord_login=args.require_discord_login,
        )
    if args.gemini_keys_config:
        validate_gemini_keys_config(args.gemini_keys_config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
