"""The event bus every view is drawn from, and the redaction applied before anything leaves it.

Every event the browser sees, live or replayed, passes through ``Bus.publish``.
Redaction happens there, once, so neither a view nor a recording can ever hold
a secret that the tracing layer registered.
"""

from __future__ import annotations

import asyncio
import base64
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

REDACTED = "‹redacted›"
# Secrets shorter than this are not searched for: replacing every "a" in a trace
# would hide everything and protect nothing.
MIN_SECRET = 6


class Redactor:
    """Replaces every registered secret, and the home directory, in any value."""

    def __init__(self) -> None:
        self._secrets: set[str] = set()
        self._home = str(Path.home())

    def add(self, secret: str | bytes) -> None:
        """Register a secret, in its own form and in the forms it may be shown in."""
        raw = secret if isinstance(secret, bytes) else secret.encode()
        forms = {raw.decode(errors="replace"), base64.b64encode(raw).decode()}
        # A PEM key: also each of its lines, should a tool print part of it.
        for line in raw.decode(errors="replace").splitlines():
            if not line.startswith("-----"):
                forms.add(line.strip())
        self._secrets |= {form for form in forms if len(form) >= MIN_SECRET}

    def scrub(self, value: Any) -> Any:
        """Return ``value`` with every secret replaced; containers are copied."""
        if isinstance(value, str):
            for secret in sorted(self._secrets, key=len, reverse=True):
                if secret in value:
                    value = value.replace(secret, REDACTED)
            if self._home and self._home != "/":
                value = value.replace(self._home, "~")
            return value
        if isinstance(value, dict):
            return {self.scrub(k): self.scrub(v) for k, v in value.items()}
        if isinstance(value, list | tuple):
            return [self.scrub(item) for item in value]
        return value


class Bus:
    """Fans events out to every open browser, and to the recording when one runs."""

    def __init__(self) -> None:
        self.redactor = Redactor()
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._taps: list[Callable[[dict[str, Any]], None]] = []
        # False while a recording plays: live events then reach no browser.
        self.live = True

    def publish(self, event: dict[str, Any], *, replayed: bool = False) -> None:
        """Redact ``event`` and send it to every subscriber and tap.

        A replayed event reaches the browsers only; a live one reaches them
        only while no recording plays, and the taps (the recorder) always.
        """
        clean = self.redactor.scrub(event)
        clean.setdefault("t", time.time())
        if self.live or replayed:
            for queue in self._subscribers:
                queue.put_nowait(clean)
        if not replayed:
            for tap in self._taps:
                tap(clean)

    def tap(self, callback: Callable[[dict[str, Any]], None]) -> Callable[[], None]:
        """Call ``callback`` with every redacted event; return what removes it."""
        self._taps.append(callback)
        return lambda: self._taps.remove(callback)

    def listen(self) -> asyncio.Queue[dict[str, Any]]:
        """Return a queue that receives every event published from now on."""
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._subscribers.add(queue)
        return queue

    def unlisten(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._subscribers.discard(queue)
