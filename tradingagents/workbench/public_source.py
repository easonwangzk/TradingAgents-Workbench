"""Deterministic, bounded source context for user-owned research image builds.

Modified for public distribution to retain attribution and dependency licenses.
"""

import argparse
import gzip
import hashlib
import io
import tarfile
from dataclasses import dataclass, field
from pathlib import Path

MAX_SOURCE_BYTES = 16 * 1024 * 1024
REQUIRED = ("pyproject.toml", "README.md", "LICENSE", "infra/Dockerfile.workbench",
            "tradingagents/workbench/worker.py", "cli/stats_handler.py")
EXCLUDED = frozenset({"__pycache__", "results", "cache", "data_cache", "checkpoints", "tests", "outputs", "private"})


@dataclass(frozen=True)
class WorkerSource:
    content: bytes = field(repr=False)

    def __post_init__(self):
        if not isinstance(self.content, bytes) or not 1 <= len(self.content) <= MAX_SOURCE_BYTES:
            raise ValueError("Invalid worker source context")

    @property
    def sha256(self):
        return hashlib.sha256(self.content).hexdigest()


def package_worker_source(root, *, max_bytes=MAX_SOURCE_BYTES):
    root = Path(root).resolve()
    if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_SOURCE_BYTES:
        raise ValueError("Invalid source size limit")
    paths = {root / relative for relative in REQUIRED}
    paths.update(root / name for name in ("NOTICE", "THIRD_PARTY_NOTICES.md") if (root / name).is_file())
    paths.update(
        path for path in (root / "licenses").glob("*")
        if path.name.endswith(("-LICENSE", "-LICENSE-MPL", "-NOTICE"))
    )
    for directory in (root / "tradingagents", root / "cli"):
        for path in directory.rglob("*.py"):
            parts = path.relative_to(root).parts
            if not any(part.startswith(".") or part.casefold() in EXCLUDED for part in parts):
                paths.add(path)
    if len(paths) > 1000:
        raise ValueError("Too many worker source files")
    output = io.BytesIO()
    total_bytes = 0
    with (gzip.GzipFile(fileobj=output, mode="wb", mtime=0) as compressed,
          tarfile.open(fileobj=compressed, mode="w") as archive):
        for path in sorted(paths):
            if (not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(root)
                    or any(parent.is_symlink() or getattr(parent, "is_junction", lambda: False)()
                           for parent in path.parents if parent != root and parent.is_relative_to(root))):
                raise ValueError("Worker source contains a missing or linked file")
            size = path.stat().st_size
            if size > 2 * 1024 * 1024 or total_bytes + size > max_bytes:
                raise ValueError("Worker source exceeds the size limit")
            content = path.read_bytes()
            total_bytes += len(content)
            if total_bytes > max_bytes:
                raise ValueError("Worker source exceeds the size limit")
            entry = tarfile.TarInfo(path.relative_to(root).as_posix())
            entry.size = len(content)
            entry.mode = 0o644
            entry.mtime = 0
            archive.addfile(entry, io.BytesIO(content))
    content = output.getvalue()
    if len(content) > max_bytes:
        raise ValueError("Worker source archive exceeds the size limit")
    return WorkerSource(content)


def package_public_source(root):
    from tradingagents.workbench.public_server import ASSETS

    root = Path(root).resolve()
    worker = package_worker_source(root)
    with tarfile.open(fileobj=io.BytesIO(worker.content), mode="r:gz") as archive:
        files = {member.name: archive.extractfile(member).read() for member in archive.getmembers()}
    files["worker-source.tar.gz"] = worker.content
    public_paths = {"infra/Dockerfile.public-workbench"} | {
        "tradingagents/workbench/static/" + asset for asset in ASSETS | {"index.html"}
    }
    for relative in sorted(public_paths):
        path = root / relative
        if (not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(root)
                or any(parent.is_symlink() or getattr(parent, "is_junction", lambda: False)()
                       for parent in path.parents if parent != root and parent.is_relative_to(root))
                or path.stat().st_size > 2 * 1024 * 1024):
            raise ValueError("Public source contains a missing, linked or oversized file")
        files[relative] = path.read_bytes()
    if sum(map(len, files.values())) > MAX_SOURCE_BYTES:
        raise ValueError("Public source exceeds the size limit")
    output = io.BytesIO()
    with (gzip.GzipFile(fileobj=output, mode="wb", mtime=0) as compressed,
          tarfile.open(fileobj=compressed, mode="w") as archive):
        for name, content in sorted(files.items()):
            entry = tarfile.TarInfo(name)
            entry.size = len(content)
            entry.mode = 0o644
            entry.mtime = 0
            archive.addfile(entry, io.BytesIO(content))
    return WorkerSource(output.getvalue())


def main():
    parser = argparse.ArgumentParser(description="Package the reviewed research worker source")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--public", action="store_true")
    parser.add_argument("--context-directory", type=Path)
    args = parser.parse_args()
    if args.context_directory and args.context_directory.exists() and any(args.context_directory.iterdir()):
        parser.error("The build context directory must be empty")
    source = package_public_source(args.root) if args.public else package_worker_source(args.root)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(source.content)
    if args.context_directory:
        args.context_directory.mkdir(parents=True, exist_ok=True)
        with tarfile.open(fileobj=io.BytesIO(source.content), mode="r:gz") as archive:
            archive.extractall(args.context_directory, filter="data")
    print(source.sha256)


if __name__ == "__main__":
    main()
