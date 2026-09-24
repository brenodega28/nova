"""Running the listener, and being able to build it again without dying.

    supervisor = Supervisor(lambda: assemble(log, log.setup), log)
    supervisor.restart()     # from any thread, at any time
    supervisor.run()         # blocks, the way a listener does

A setting that changes which Whisper model transcribes is not a setting until
something reloads the model, and nothing here can be reconfigured in place: the
weights are on the GPU, the microphone stream is open, and the objects holding
both are built once in ``assemble`` and wired together permanently. Rebuilding
them is the honest way to apply a change, so this is a loop around that.

It is deliberately not a process manager. The API is started and stopped
separately from the backend, so it cannot restart this process — it can only ask
this process to restart itself, which is :meth:`restart` (drop the listener,
build a new one, keep the interpreter and anything cached in it) or
:meth:`reload` (replace the process image, paying the full model load again).
The first is what a settings change wants; the second is what a new version of
the code wants.

It satisfies the same protocol a :class:`listener.Listener` does — a
``stop_event`` and a ``run``, which is all :mod:`ui` ever asked for — so the
screen neither knows nor cares that what it is running can now be rebuilt
underneath it.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from collections.abc import Callable

from hit_log import HitLog

REBUILD_PAUSE = 0.5


class Supervisor:
    """Builds a listener, runs it, and builds another when asked to."""

    def __init__(self, build: Callable[[], object], log: HitLog):
        self.build = build
        self.log = log
        self.stop_event = threading.Event()
        self.listener = None
        self.generation = 0
        self._restarting = threading.Event()
        self._reloading = threading.Event()
        self._running = threading.Event()

    def restart(self) -> None:
        """Drop the current listener and build a fresh one. Never blocks.

        Called from a control-plane thread while the listener is mid-sentence,
        so it asks rather than tears down: the listener winds itself up the way
        it does for a quit, and the loop in :meth:`run` notices why.
        """
        self._restarting.set()
        self.stop_event.set()

    def reload(self) -> None:
        """Replace the process image once the listener has wound down."""
        self._reloading.set()
        self.restart()

    def wait_until_running(self, timeout: float = 120.0) -> bool:
        return self._running.wait(timeout)

    def _handover(self) -> None:
        """Stand the process up again from scratch, in place.

        ``execv`` rather than a fresh child, so whatever started the backend
        keeps the same process to supervise and the control port is reopened by
        something that inherits its place in the world.
        """
        self.log.error("reloading — the port will be closed briefly")
        sys.stdout.flush()
        sys.stderr.flush()
        os.execv(sys.executable, [sys.executable, *sys.argv])

    def run(self) -> int:
        """Run listeners, one after another, until asked to stop for good."""
        while True:
            self.stop_event.clear()
            self._restarting.clear()
            self._running.clear()

            self.log.starting()
            try:
                self.listener = self.build()
            except Exception as exc:
                self.listener = None
                self.log.broken(f"{type(exc).__name__}: {exc}")
                if not self._wait_for_another_try():
                    return 1
                continue

            self.listener.stop_event = self.stop_event
            self.generation += 1
            self._running.set()
            try:
                self.listener.run()
            finally:
                self._running.clear()
                self.listener = None

            if self._reloading.is_set():
                self._handover()
            if not self._restarting.is_set():
                return 0
            time.sleep(REBUILD_PAUSE)

    def _wait_for_another_try(self) -> bool:
        """Setup failed. Stay up so the dashboard can say why, and be told to retry.

        Exiting here would take the control port down with it, which is the
        moment a dashboard most needs it: something is wrong, and the only way to
        find out what is to ask. So it waits, and a ``restart`` from the API is
        what tries again.
        """
        while not self._restarting.is_set():
            if self.stop_event.wait(timeout=0.25) and not self._restarting.is_set():
                return False
        return True
