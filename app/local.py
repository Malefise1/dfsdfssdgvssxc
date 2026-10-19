"""Local process coordination for maintenance tasks on Linux."""

from contextlib import contextmanager


@contextmanager
def file_lock(path):
    import fcntl

    stream = path.open("a")
    path.chmod(0o600)
    try:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("Операция уже выполняется другим процессом") from exc
        yield
    finally:
        stream.close()
