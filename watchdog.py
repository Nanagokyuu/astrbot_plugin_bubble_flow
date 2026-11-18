"""Per-turn watchdog tasks that release stalled session flight locks."""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable


class FlightWatchdog:
    """Owns the asyncio timers that auto-release a leaked flight lock."""

    def __init__(
        self,
        on_expire: Callable[[str, str, float], Awaitable[None]],
    ) -> None:
        self._on_expire = on_expire
        self._tasks: dict[tuple[str, str], asyncio.Task[Any]] = {}

    def start(self, umo: str, token: str, timeout_seconds: float) -> None:
        if not umo or not token or timeout_seconds <= 0:
            return
        self.cancel(umo, token)
        key = (umo, token)
        self._tasks[key] = asyncio.create_task(
            self._run(umo, token, timeout_seconds, key)
        )

    def cancel(self, umo: str, token: str = "") -> None:
        """Cancel one turn's timer, or every timer of a session without token."""
        keys = [(umo, token)] if token else [key for key in self._tasks if key[0] == umo]
        for key in keys:
            task = self._tasks.pop(key, None)
            if task and not task.done():
                task.cancel()

    def close(self) -> None:
        for task in list(self._tasks.values()):
            if not task.done():
                task.cancel()
        self._tasks.clear()

    async def _run(
        self, umo: str, token: str, timeout_seconds: float, key: tuple[str, str]
    ) -> None:
        try:
            await asyncio.sleep(timeout_seconds)
            await self._on_expire(umo, token, timeout_seconds)
        except asyncio.CancelledError:
            return
        finally:
            if self._tasks.get(key) is asyncio.current_task():
                self._tasks.pop(key, None)
