"""Read the tail of an append-only JSONL log written by a runtime."""

from pathlib import Path


def tail_lines(path: Path, tail_bytes: int, since_bytes: int = 0) -> list[bytes]:
    """Lines in the last `tail_bytes`, never before `since_bytes`; a line cut by the tail start is dropped.

    Args:
        path: The log file.
        tail_bytes: How many bytes from the end to read at most.
        since_bytes: Offset already consumed; reading never starts before it.

    Returns:
        The lines in the read range, without line endings.
    """
    with path.open("rb") as handle:
        size = handle.seek(0, 2)
        offset = max(0, size - tail_bytes, since_bytes)
        handle.seek(offset)
        if offset and offset != since_bytes:
            handle.readline()
        return handle.read().splitlines()
