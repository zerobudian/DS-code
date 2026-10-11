"""Small atomic file writer shared by project data and local API uploads."""

import os
import tempfile
from pathlib import Path


def atomic_write_bytes(path: Path, payload: bytes) -> bool:
    target = Path(path)
    if target.is_symlink() or target.parent.is_symlink():
        return False
    directory = None
    temp_name = None
    try:
        if os.open in os.supports_dir_fd and os.rename in os.supports_dir_fd:
            directory = os.open(target.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                                | getattr(os, "O_NOFOLLOW", 0))
            temp_name = f".tmp_{os.getpid()}_{os.urandom(12).hex()}"
            fd = os.open(temp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                         | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=directory)
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_name, target.name, src_dir_fd=directory, dst_dir_fd=directory)
        else:
            # Windows has no directory-fd API; still never fall back to a partial write.
            fd, temp_name = tempfile.mkstemp(prefix=".tmp_", dir=target.parent)
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            if target.is_symlink() or target.parent.is_symlink():
                return False
            os.replace(temp_name, target)
        return True
    except (OSError, ValueError, TypeError):
        return False
    finally:
        if temp_name:
            try:
                if directory is not None:
                    os.unlink(temp_name, dir_fd=directory)
                else:
                    os.unlink(temp_name)
            except OSError:
                pass
        if directory is not None:
            os.close(directory)
