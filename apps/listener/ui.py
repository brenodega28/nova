"""The terminal interface: a face up top, the conversation running underneath.

Every name added to a Textual subclass here is prefixed or renamed away from the
framework's own: ``_closing``, ``_thread``, ``_build`` and ``stop`` all belong to
``MessagePump``, and quietly reusing one breaks the shutdown state machine rather
than raising anything.

Textual owns the main thread, so the listener runs in a worker and everything it
has to say arrives through :class:`UiLog` — a :class:`hit_log.HitLog` whose
console printing is swapped for calls marshalled onto the UI thread. The JSONL
log it inherits is untouched, so a run records the same events either way.

The face is the point of the whole screen. Knowing whether she heard you used to
mean reading timestamps; here her eyes widen when she wakes, narrow while she
thinks, and her mouth moves while she talks.

Her name is never written here — it comes from :mod:`persona`, so renaming her
does not mean editing the interface.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Protocol

import persona
from rich.markup import escape
from textual.app import App, ComposeResult
from textual.containers import Center, VerticalScroll
from textual.css.query import NoMatches
from textual.message import Message
from textual.reactive import reactive
from textual.theme import Theme
from textual.widgets import Footer, Static

from hit_log import HitLog

class Listening(Protocol):
    """What the interface needs of a listener: run it, and ask it to stop.

    Stated as a protocol rather than an import so the screen stays ignorant of
    the wake-word machinery it happens to be showing.
    """

    stop_event: threading.Event

    def run(self) -> int: ...


LOADING = "loading"
IDLE = "idle"
LISTENING = "listening"
THINKING = "thinking"
SPEAKING = "speaking"
BROKEN = "broken"

HEAD = """\
    ╭╮       ╭╮
 ╭──┴┴───────┴┴──╮
 │   {eye}       {eye}   │
 │       {mouth}       │
 ╰───────────────╯"""

FACES = {
    LOADING: ("·", "·‥⋯‥", "waking up"),
    IDLE: ("●", "‿", "idle"),
    LISTENING: ("◉", "○", "listening"),
    THINKING: ("–", "~≈-≈", "thinking"),
    SPEAKING: ("●", "▽○▽●", "speaking"),
    BROKEN: ("x", "·", "trouble"),
}

BLUE = Theme(
    name="nova-blue",
    primary="#0178d4",
    secondary="#004578",
    accent="#4aa8ff",
    warning="#7fb2ff",
    error="#ba3c5b",
    success="#4ebf71",
    foreground="#e0e0e0",
    dark=True,
)

TICK_SECONDS = 0.22
FACE_WIDTH = 18
SHUTDOWN_SECONDS = 5.0


class Draw(Message):
    """A request from the listener's thread to change what is on screen.

    Posting a message rather than calling ``call_from_thread`` keeps the worker
    from blocking: that call waits for the event loop to run the callback, so one
    issued while the app is shutting down waits for a loop that is never going to
    run it again, and the app in turn waits for the worker. Messages queued after
    shutdown are simply dropped.
    """

    def __init__(self, action, args: tuple):
        super().__init__()
        self.action = action
        self.args = args


class Face(Static):
    """The assistant, drawn as a head that reacts to what she is doing."""

    state = reactive(LOADING)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._frame = 0
        self._note = ""

    def on_mount(self) -> None:
        self.set_interval(TICK_SECONDS, self._advance)
        self._redraw()

    def watch_state(self) -> None:
        self._frame = 0
        self._redraw()

    def note(self, text: str) -> None:
        """Replace the caption under the face, e.g. with the gate reading."""
        self._note = text
        self._redraw()

    def _advance(self) -> None:
        self._frame += 1
        self._redraw()

    def _redraw(self) -> None:
        eye, mouths, label = FACES.get(self.state, FACES[IDLE])
        mouth = mouths[self._frame % len(mouths)]
        lines = [HEAD.format(eye=eye, mouth=mouth), label.center(FACE_WIDTH)]
        if self._note:
            lines.append(self._note.center(FACE_WIDTH))
        self.update("\n".join(lines))


class Chat(VerticalScroll):
    """Everything said, in the order it was said."""

    def say(self, markup: str, kind: str) -> Static:
        line = Static(markup, classes=f"bubble {kind}")
        self.mount(line)
        self.scroll_end(animate=False)
        return line


class AssistantApp(App):
    """Wraps the listener in a screen: the face, then the conversation."""

    TITLE = persona.NAME
    CSS = """
    Screen { background: $surface; }
    #head { height: auto; padding: 1 0 0 0; }
    Face { width: auto; height: auto; color: $accent; }
    Chat { height: 1fr; padding: 1 2 0 2; }
    .bubble { height: auto; margin: 0 0 1 0; }
    .you { color: $text; }
    .reply { color: $success; }
    .system { color: $accent-darken-1; text-style: italic; }
    .oops { color: $error; }
    """
    BINDINGS = [
        ("q", "shutdown", "quit"),
        ("ctrl+c", "shutdown", "quit"),
    ]

    def __init__(
        self,
        build: Callable[["AssistantApp"], Listening],
        wake_word: str = persona.WAKE_WORD,
        assistant_name: str = persona.NAME,
    ):
        super().__init__()
        self._assemble_listener = build
        self.listener: Listening | None = None
        self.wake_word = wake_word
        self.assistant_name = assistant_name
        self._listener_thread: threading.Thread | None = None
        self._winding_down = False
        self._listener_started = False
        self._answer: Static | None = None
        self._answer_text = ""

    def compose(self) -> ComposeResult:
        with Center(id="head"):
            yield Face(id="face")
        yield Chat(id="chat")
        yield Footer()

    @property
    def face(self) -> Face:
        return self.query_one(Face)

    @property
    def chat(self) -> Chat:
        return self.query_one(Chat)

    def on_mount(self) -> None:
        self.register_theme(BLUE)
        self.theme = BLUE.name
        if self._listener_started:
            return
        self._listener_started = True
        self._listener_thread = threading.Thread(target=self._work, daemon=True)
        self._listener_thread.start()

    def request_stop(self) -> None:
        """Ask the listener to wind down. Never blocks.

        Anything called from a binding or an event handler runs on the thread
        drawing the screen, so it must not wait for the listener: blocking that
        thread freezes the interface and leaves ``exit()`` unable to take effect.
        Waiting happens in :meth:`stop`, once the event loop is done with.
        """
        self._winding_down = True
        if self.listener is not None:
            self.listener.stop_event.set()

    def shutdown_listener(self) -> None:
        """Wait for the listener to finish, from outside the event loop.

        The listener runs on a daemon thread of its own rather than a Textual
        worker, so nothing here can wedge the app's own shutdown; this only gives
        the audio devices a moment to close before the process goes away.
        """
        self.request_stop()
        thread, self._listener_thread = self._listener_thread, None
        if thread is not None:
            thread.join(timeout=SHUTDOWN_SECONDS)

    def on_draw(self, event: Draw) -> None:
        """Apply a queued change, unless the screen it would touch is gone.

        The listener keeps talking for as long as it takes to wind down, so a
        message can still arrive after the widgets have been taken apart.
        """
        if self._winding_down:
            return
        try:
            event.action(*event.args)
        except NoMatches:
            pass

    def _work(self) -> None:
        """Assemble and run the listener, off the thread drawing the screen."""
        try:
            self.listener = self._assemble_listener(self)
            if self._winding_down:
                return
            self.listener.run()
        except Exception as exc:
            self.post_message(Draw(self.add_error, (f"{type(exc).__name__}: {exc}",)))
            self.post_message(Draw(self.set_state, (BROKEN,)))

    def action_shutdown(self) -> None:
        self.request_stop()
        self.exit()

    def on_unmount(self) -> None:
        self.request_stop()

    def set_state(self, state: str) -> None:
        self.face.state = state

    def add_setup(self, text: str) -> None:
        self.chat.say(f"· {escape(text)}", "system")

    def add_system(self, text: str) -> None:
        self.chat.say(f"· {escape(text)}", "system")

    def add_error(self, text: str) -> None:
        self.chat.say(f"! {escape(text)}", "oops")

    def show_ready(self, floor: float, threshold: float) -> None:
        self.set_state(IDLE)
        self.face.note(f"gate {threshold:.4f}")
        self.add_system(f"microphone live, noise floor {floor:.4f} rms")

    def show_wake(self, clock: str, hits: int, latency: float) -> None:
        self.set_state(LISTENING)
        self.add_system(
            f"{clock} · woke on '{self.wake_word}' #{hits} ({latency * 1000:.0f} ms)"
        )

    def add_question(self, text: str) -> None:
        self._answer = None
        self._answer_text = ""
        self.chat.say(f"[b]you[/b]  {escape(text)}", "you")

    def add_answer_chunk(self, text: str) -> None:
        self.set_state(SPEAKING)
        self._answer_text = f"{self._answer_text} {text}".strip()
        if self._answer is None:
            self._answer = self.chat.say(
                f"[b]{self.assistant_name}[/b]  {escape(self._answer_text)}", "reply"
            )
        else:
            self._answer.update(
                f"[b]{self.assistant_name}[/b]  {escape(self._answer_text)}"
            )
            self.chat.scroll_end(animate=False)

    def finish_answer(self, elapsed: float) -> None:
        self.set_state(IDLE)
        if self._answer is not None:
            self._answer.update(
                f"[b]{self.assistant_name}[/b]  {escape(self._answer_text)}"
                f"  [dim]({elapsed:.1f}s)[/dim]"
            )
        self._answer = None
        self._answer_text = ""

    def show_timeout(self) -> None:
        self.set_state(IDLE)
        self.add_system("no question — back to listening")


class UiLog(HitLog):
    """A :class:`HitLog` that draws on the screen instead of the console."""

    def __init__(self, app: AssistantApp, wake_word: str, **kwargs):
        super().__init__(wake_word, **kwargs)
        self.app = app

    def _post(self, action: Callable, *args) -> None:
        try:
            self.app.post_message(Draw(action, args))
        except Exception:
            pass

    def setup(self, text: str) -> None:
        self._post(self.app.add_setup, text)

    def ready(self, floor: float, threshold: float) -> None:
        self._post(self.app.show_ready, floor, threshold)

    def scanned(self, utterance, text: str) -> None:
        if self.verbose:
            self._post(self.app.add_system, f"~ {text}")

    def wake(self, utterance, text: str, latency: float) -> None:
        self.hits += 1
        self._post(self.app.show_wake, self._clock(utterance), self.hits, latency)
        self._append("wake", utterance, text, latency=latency)

    def question(self, utterance, text: str) -> None:
        self._post(self.app.add_question, text)
        self._append("question", utterance, text)

    def thinking(self) -> None:
        self._post(self.app.set_state, THINKING)

    def answer_chunk(self, text: str) -> None:
        self._post(self.app.add_answer_chunk, text)

    def answer(self, text: str, elapsed: float) -> None:
        self._post(self.app.finish_answer, elapsed)
        self._append("answer", None, text, latency=elapsed)

    def timeout(self) -> None:
        self._post(self.app.show_timeout)

    def error(self, message: str) -> None:
        self._post(self.app.add_error, message)

    def summary(self) -> None:
        self._post(self.app.add_system, f"heard '{self.wake_word}' {self.hits} time(s)")
