"""Thinking: the local LLM that answers whatever the listener heard.

Ollama serves the model over HTTP on localhost, so nothing here needs a client
library — the chat endpoint streams newline-delimited JSON and the standard
library reads it.

    import model

    brain = model.Model("qwen3:14b")
    brain.load()
    for sentence in brain.stream("What is the capital of Portugal?"):
        print(sentence)

Answers are spoken rather than read, which shapes the whole module.
:meth:`Model.stream` hands back one sentence at a time, so the first can be
playing through the speakers while the model is still generating the rest.
Markup is stripped, because asterisks and backticks are noise out loud. And
Qwen-style models reason at length before answering, which is dead air for a
voice assistant, so thinking stays off unless it is asked for.
"""

from __future__ import annotations

import json
import re
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator

import persona

DEFAULT_HOST = "http://localhost:11434"
DEFAULT_MODEL = "qwen3:14b"
DEFAULT_SYSTEM = persona.SYSTEM
DEFAULT_HISTORY_TURNS = 6
DEFAULT_TIMEOUT = 120.0

SENTENCE_END = re.compile(r"[.!?…]['\"”’)\]]*(?=\s)|\n")
THINK_BLOCK = re.compile(r"<think>.*?</think>", re.S)
CODE_FENCE = re.compile(r"```.*?```", re.S)
MARKUP = re.compile(r"[*_`#>|]+")
BULLET = re.compile(r"^\s*(?:[-•·]|\d+[.)])\s+", re.M)


class ModelError(RuntimeError):
    """The model could not be reached, or refused to answer."""


def speakable(text: str) -> str:
    """Reduce model output to something worth sending to a text to speech voice."""
    text = THINK_BLOCK.sub(" ", text)
    text = CODE_FENCE.sub(" ", text)
    text = BULLET.sub("", text)
    text = MARKUP.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


def split_sentence(buffer: str) -> tuple[str, str]:
    """Peel the first complete sentence off ``buffer``.

    Returns ``("", buffer)`` while the sentence is still being generated.
    """
    match = SENTENCE_END.search(buffer)
    if match is None:
        return "", buffer
    return buffer[: match.end()].strip(), buffer[match.end() :]


class Model:
    """A chat model running under Ollama, remembering the last few turns."""

    def __init__(
        self,
        name: str = DEFAULT_MODEL,
        host: str = DEFAULT_HOST,
        system: str = DEFAULT_SYSTEM,
        think: bool = False,
        history_turns: int = DEFAULT_HISTORY_TURNS,
        timeout: float = DEFAULT_TIMEOUT,
    ):
        self.name = name
        self.host = host.rstrip("/")
        self.system = system
        self.think = think
        self.history_turns = history_turns
        self.timeout = timeout

        self._lock = threading.Lock()
        self._history: list[dict[str, str]] = []
        self._capabilities: list[str] | None = None

    def _post(self, path: str, payload: dict):
        request = urllib.request.Request(
            f"{self.host}{path}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            return urllib.request.urlopen(request, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace").strip()
            raise ModelError(f"ollama refused {path}: {detail or exc.reason}") from exc
        except urllib.error.URLError as exc:
            raise ModelError(
                f"cannot reach ollama at {self.host} ({exc.reason}) — "
                "is 'ollama serve' running?"
            ) from exc

    def capabilities(self) -> list[str]:
        with self._lock:
            if self._capabilities is None:
                with self._post("/api/show", {"model": self.name}) as response:
                    shown = json.load(response)
                self._capabilities = list(shown.get("capabilities") or ())
            return self._capabilities

    def supports_thinking(self) -> bool:
        return "thinking" in self.capabilities()

    def load(self) -> None:
        """Pull the weights into memory so the first real answer is not slow."""
        self.capabilities()
        payload = self._payload([{"role": "user", "content": "hi"}], stream=False)
        payload["options"] = {"num_predict": 1}
        with self._post("/api/chat", payload) as response:
            json.load(response)

    def forget(self) -> None:
        with self._lock:
            self._history.clear()

    def _payload(self, messages: list[dict[str, str]], stream: bool) -> dict:
        payload = {"model": self.name, "messages": messages, "stream": stream}
        if self.supports_thinking():
            payload["think"] = self.think
        return payload

    def _messages(self, question: str) -> list[dict[str, str]]:
        with self._lock:
            history = list(self._history)
        messages = [{"role": "system", "content": self.system}] if self.system else []
        return messages + history + [{"role": "user", "content": question}]

    def _remember(self, question: str, answer: str) -> None:
        if not answer:
            return
        with self._lock:
            self._history += [
                {"role": "user", "content": question},
                {"role": "assistant", "content": answer},
            ]
            del self._history[: max(0, len(self._history) - self.history_turns * 2)]

    def _chat(self, messages: list[dict[str, str]]) -> Iterator[dict]:
        with self._post("/api/chat", self._payload(messages, stream=True)) as response:
            for line in response:
                line = line.strip()
                if not line:
                    continue
                chunk = json.loads(line)
                if chunk.get("error"):
                    raise ModelError(str(chunk["error"]))
                yield chunk

    def stream(self, question: str) -> Iterator[str]:
        """Answer ``question``, yielding each sentence as soon as it is complete."""
        question = question.strip()
        if not question:
            return
        buffer = ""
        said: list[str] = []
        for chunk in self._chat(self._messages(question)):
            delta = (chunk.get("message") or {}).get("content") or ""
            if not delta:
                continue
            buffer += delta
            while True:
                sentence, buffer = split_sentence(buffer)
                if not sentence:
                    break
                sentence = speakable(sentence)
                if sentence:
                    said.append(sentence)
                    yield sentence
        tail = speakable(buffer)
        if tail:
            said.append(tail)
            yield tail
        self._remember(question, " ".join(said))

    def answer(self, question: str) -> str:
        """Answer ``question`` and return the whole thing."""
        return " ".join(self.stream(question))


if __name__ == "__main__":
    import sys

    brain = Model()
    for part in brain.stream(" ".join(sys.argv[1:]) or "Who are you, in one line?"):
        print(part, flush=True)
