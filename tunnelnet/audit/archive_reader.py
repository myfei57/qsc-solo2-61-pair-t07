"""Read records back out of sealed archive packages.

Packages are immutable: this module never writes, it only verifies the tar
against its manifest and decodes the JSONL members.
"""

from __future__ import annotations

import hashlib
import json
import os
import tarfile

from . import layout, manifest as manifest_module
from .model import Record
from .segment import Segment

HASH_CHUNK = 64 * 1024


def package_path(root: str, package_manifest: manifest_module.PackageManifest) -> str:
    return os.path.join(layout.archive_dir(root), package_manifest.package_file)


def package_exists(root: str, package_manifest: manifest_module.PackageManifest) -> bool:
    return os.path.exists(package_path(root, package_manifest))


def verify_package(root: str, package_manifest: manifest_module.PackageManifest) -> bool:
    """Re-hash the tar and every member; return whether the package is intact."""

    path = package_path(root, package_manifest)
    if not os.path.exists(path):
        return False
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            block = handle.read(HASH_CHUNK)
            if not block:
                break
            digest.update(block)
    if digest.hexdigest() != package_manifest.tar_sha256:
        return False
    with tarfile.open(path, mode="r:gz") as tar:
        members = {member.name: member for member in tar.getmembers()}
        for entry in package_manifest.segments:
            member = members.get(entry.path)
            if member is None:
                return False
            extracted = tar.extractfile(member)
            if extracted is None:
                return False
            if hashlib.sha256(extracted.read()).hexdigest() != entry.sha256:
                return False
    return True


def read_package(
    root: str,
    package_manifest: manifest_module.PackageManifest,
    verify: bool = True,
) -> list[Record]:
    """Decode every record in one sealed package."""

    if verify and not verify_package(root, package_manifest):
        raise ArchiveIntegrityError(package_manifest.package_id)
    path = package_path(root, package_manifest)
    records: list[Record] = []
    with tarfile.open(path, mode="r:gz") as tar:
        for member in sorted(tar.getmembers(), key=lambda item: item.name):
            if not member.isfile() or not member.name.endswith(".jsonl"):
                continue
            extracted = tar.extractfile(member)
            if extracted is None:
                continue
            for raw_line in extracted.read().decode("utf-8").splitlines():
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    document = json.loads(line)
                except ValueError:
                    continue
                if isinstance(document, dict) and {"id", "event", "at"} <= set(document):
                    records.append(
                        Record.from_document(document, origin=f"archive:{package_manifest.package_id}")
                    )
    return records


class ArchiveIntegrityError(RuntimeError):
    """Raised when a sealed package fails checksum verification."""
