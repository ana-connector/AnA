"""Turn tracing, optionally shipped to a self-hosted Langfuse.

The app never imports the Langfuse SDK directly. This wrapper turns into a
no-op when LANGFUSE_HOST and keys are absent, which keeps the default
deployment from depending on a service being up and keeps tests hermetic —
a tracing layer that can break a turn is worse than no tracing layer.
"""
import os
import time
from contextlib import contextmanager
from typing import Any, Optional


class NullSpan:
    def update(self, **kwargs: Any) -> None:
        return None

    def end(self, **kwargs: Any) -> None:
        return None


class NullTrace:
    def span(self, **kwargs: Any) -> NullSpan:
        return NullSpan()

    def update(self, **kwargs: Any) -> None:
        return None


class Tracer:
    """Creates one trace per turn, with spans inside it.

    `client` is injected in tests; in production it is built from the
    environment by `from_env()`."""

    def __init__(self, client: Optional[Any] = None):
        self.client = client

    @property
    def enabled(self) -> bool:
        return self.client is not None

    @classmethod
    def from_env(cls) -> "Tracer":
        host = os.environ.get("LANGFUSE_HOST")
        public_key = os.environ.get("LANGFUSE_PUBLIC_KEY")
        secret_key = os.environ.get("LANGFUSE_SECRET_KEY")
        if not (host and public_key and secret_key):
            return cls(None)
        try:
            from langfuse import Langfuse  # imported lazily: optional dependency
        except ImportError:
            return cls(None)
        try:
            return cls(Langfuse(host=host, public_key=public_key, secret_key=secret_key))
        except Exception:  # noqa: BLE001 - misconfiguration must not break chat
            return cls(None)

    def trace(self, *, name: str, thread_id: str, metadata: Optional[dict] = None):
        if not self.enabled:
            return NullTrace()
        try:
            return self.client.trace(
                name=name, session_id=thread_id, metadata=metadata or {}
            )
        except Exception:  # noqa: BLE001 - see class docstring
            return NullTrace()

    @contextmanager
    def span(self, trace_obj: Any, *, name: str, metadata: Optional[dict] = None):
        """Times a section and attaches it to `trace_obj`.

        Always yields — a failing tracer yields a NullSpan rather than raising,
        so a span is never a reason a turn fails."""
        started = time.time()
        span: Any = NullSpan()
        try:
            span = trace_obj.span(name=name, metadata=metadata or {})
        except Exception:  # noqa: BLE001
            span = NullSpan()
        try:
            yield span
        finally:
            with _suppressed():
                span.end(metadata={"duration_seconds": round(time.time() - started, 3)})

    def flush(self) -> None:
        if not self.enabled:
            return
        with _suppressed():
            self.client.flush()


@contextmanager
def _suppressed():
    try:
        yield
    except Exception:  # noqa: BLE001 - tracing failures are never fatal
        pass
