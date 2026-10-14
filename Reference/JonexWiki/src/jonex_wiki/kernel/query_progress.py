"""Optional, task-local public progress; never feeds back into query decisions."""
from contextlib import contextmanager
from contextvars import ContextVar
import logging

_listener = ContextVar("wiki_query_progress", default=None)


def publish_query_progress(event):
    listener = _listener.get()
    if listener is not None:
        try:
            listener(event)
        except Exception:
            logging.getLogger(__name__).warning("query progress listener failed", exc_info=True)


@contextmanager
def query_progress(listener):
    token = _listener.set(listener)
    try:
        yield
    finally:
        _listener.reset(token)
