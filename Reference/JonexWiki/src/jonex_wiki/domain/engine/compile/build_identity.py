"""Installed Python implementation identity, sampled once per process."""
from functools import lru_cache
import hashlib
from pathlib import Path


@lru_cache(maxsize=2)
def implementation_checksum(*, compilation=False):
    root = Path(__file__).resolve().parents[1]
    digest = hashlib.sha256()
    for path in sorted(root.rglob('*.py')):
        relative = path.relative_to(root).as_posix()
        # These query modules also build published content/indexes. Other
        # query modules only execute against an already published revision.
        if compilation and relative.startswith('query/') and relative not in {
            'query/retrieval.py', 'query/table_reader.py', 'query/algorithm.py',
        }:
            continue
        digest.update(relative.encode())
        digest.update(b'\0')
        digest.update(path.read_bytes())
        digest.update(b'\0')
    return digest.hexdigest()
