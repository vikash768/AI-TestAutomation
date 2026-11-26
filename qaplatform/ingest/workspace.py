"""Temporary, worker-local storage for private Git checkouts."""
from __future__ import annotations

import logging
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import mkdtemp

logger = logging.getLogger(__name__)


@contextmanager
def repository_workspace(artifacts_dir: Path) -> Iterator[Path]:
    """Create and clean up one checkout attempt outside the artifact directory."""
    artifacts_dir = artifacts_dir.resolve()
    private_dir = artifacts_dir.with_name(f".{artifacts_dir.name}-repos")
    if private_dir.is_symlink():
        raise ValueError("Private repository directory must not be a symlink")
    private_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    workspace = Path(mkdtemp(prefix="run-", dir=private_dir))
    try:
        yield workspace
    finally:
        try:
            shutil.rmtree(workspace)
        except FileNotFoundError:
            pass
        except OSError:
            logger.warning("Could not remove private repository workspace", exc_info=True)
