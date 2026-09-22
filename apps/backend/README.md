# ollie

Listens on the microphone, flags the moment you say **"ollie"**, transcribes
the question you ask next, and answers it out loud. Everything runs locally —
Whisper for the ears, Ollama for the thinking, Piper for the voice.

Two Whisper models share the work. A small one (`base.en`) scans rolling
windows of audio *while you are still speaking*, so the wake word lands in
around **0.2 s**. A large one (`large-v3`) then transcribes the question, where
accuracy matters and a little latency does not.

```
ollie: "Hi, I'm Ollie, how can I help you?"  once the mic is calibrated
you:   "Ollie."
      └ flagged ~0.2s later
ollie: "Yes?"                    spoken by Piper, ~0.2s to first sound
you:   "What is the weather in Lisbon today?"
ollie: "Let me think."           immediately, before transcription starts
      └ transcribed by large while that plays, printed and logged
ollie: "I can't check live weather…"
      └ answered by qwen3 via Ollama, spoken sentence by sentence
```

The microphone hears her own voice, so capture is gated while she speaks:
`talk.muted_until()` reports when input can be trusted again, and the listener
throws away anything captured before then. The trade-off is no barge-in — wait
for her to finish before asking.

Saying it all in one breath works too — the wake word still fires mid-sentence,
and the rest of that utterance is taken as the question.

## Her persona

Everything that makes her *her* lives in one file:

```python
# persona.py
NAME = "Ollie"
MISHEARINGS = ("olly", "oli", "oly", "olli", "ollee", "ollie's")
VOICE = "en_US-ryan-medium"
SYSTEM = "You are Ollie, a voice assistant. Every reply you give is spoken…"
GREETING = "Hi, I'm Ollie, how can I help you?"
WAKE_REPLY = "Yes?"
THINKING_REPLIES = ("Let me think.", "Hmm, let me think.", "One moment.")
```

Change `NAME` and the rest follows — the wake word, the greeting, the prompt, the
label on her side of the conversation, the window title. Nothing else in the
project spells out her name, her prompt, her voice, or a single word she says
unprompted, and every one of them can still be overridden per session from the
command line.

`MISHEARINGS` is the part a rename does not carry over, because it depends on how
the name *sounds*. It matters more than it looks: on the fuzzy match alone,
"ollie" is recognised but "olly", "oli" and "oly" are not, and Whisper produces
all three. A new name works with none listed, so start there and add what
`--verbose` shows you actually being misheard as. Short names trip over ordinary
speech more easily, so watch for false wakes before settling on one.

## Layout

| file | holds |
| --- | --- |
| `main.py` | the CLI — flags, device choice, model loading, entry point |
| `audio.py` | microphone capture, the voice activity gate, rolling partials |
| `hit_log.py` | console output and the optional JSONL log |
| `persona.py` | her character — name, prompt, voice, and everything she says unprompted |
| `listener.py` | wake-word matching and the wake → question state machine |
| `ui.py` | the Textual screen — the face, the conversation, the log that feeds them |
| `model.py` | the local LLM — Ollama over HTTP, answers streamed by sentence |
| `talk.py` | speech out — Piper neural TTS played to the speakers |

The interface holds the main thread, capture runs on a thread of its own, wake
scanning on a second, and the large model on a third. The wake scanner keeps
only the newest window, so a slow pass never leaves it chewing through stale
audio.

## The screen

`main.py` opens a Textual interface: the assistant up top, the conversation
underneath, both driven by the same events that used to scroll past as console
lines.

```
        ╭╮       ╭╮
     ╭──┴┴───────┴┴──╮
     │   ◉       ◉   │
     │       ○       │
     ╰───────────────╯
         listening
        gate 0.0069

  · microphone live, noise floor 0.0023 rms
  · 20:00:00 · woke on 'ollie' #1 (38 ms)
  you   What is the capital of Portugal?
  Ollie  The capital of Portugal is Lisbon.  (2.0s)
```

The face is the part worth having. Her eyes widen the moment she wakes, narrow
and her mouth moves while she thinks, open while she speaks — so "did she hear
me?" is answered by glancing up rather than by reading timestamps.

| state | eyes | meaning |
| --- | --- | --- |
| waking up | `·` | models still loading |
| idle | `●` | listening for the wake word |
| listening | `◉` | awake, waiting for your question |
| thinking | `–` | transcribing, or the model is generating |
| speaking | `●` | audio is playing |
| trouble | `x` | setup failed — the reason is in the log below |

Textual owns the main thread, so the listener runs on a thread of its own and
everything it has to say is posted to the screen as a message. `ui.UiLog` is a
`HitLog` with the printing swapped out, so the JSONL written by `--log` is
identical in either mode. Press `q` to quit.

Use `--plain` for the old scrolling console output — worth it when piping to a
file or debugging something the interface would paint over.

## Thinking

The question goes to a model running under [Ollama](https://ollama.com), which
needs to be up (`ollama serve`) with the model pulled:

```sh
ollama pull qwen3:14b
```

```python
import model

brain = model.Model("qwen3:14b")
brain.load()                          # pull the weights into memory up front
for sentence in brain.stream("Why is the sky blue?"):
    print(sentence)                   # one complete sentence at a time
brain.answer("And at sunset?")        # the whole thing, remembering the last turn
brain.forget()                        # drop the conversation
```

`stream()` yields whole sentences rather than tokens, because the point is to
start *speaking* before the model has finished: the first sentence of a
three-sentence answer is ready in about a third of the total time. The gate that
mutes the microphone is held across the entire answer, so the gaps between
sentences are not mistaken for your turn to speak.

Between your question ending and the answer starting there are two or three
seconds of silence, which is indistinguishable from not having been heard. So
she fills it — "let me think", picked at random from `--thinking-reply` — the
instant the question lands, *before* the large model transcribes it:

```
0.00s  "Hmm, let me think."        ← spoken straight away
1.50s  transcribed                 ← whisper large, while the filler plays
2.45s  "Earth is the third…"       ← first sentence of the answer
```

Transcription and generation both run while the filler plays, so it costs no
added delay — the answer arrives marginally *sooner* than it did with nothing
there, because the filler no longer sits in series with the model call. Pass
`--thinking-reply ''` to go straight to the answer.

Saying only the wake word does not trigger it. For a one-breath "Ollie, what's
the weather", the cheap model re-scans the burst first (about 0.1 s) to confirm
a question was actually asked, so "Ollie." on its own stays quiet rather than
promising to think about nothing.

Answers are spoken, not read, which shapes two more things. Markdown is stripped
before synthesis — asterisks and backticks are noise out loud. And Qwen-style
models reason at length before answering, which is dead air, so thinking is off
unless you pass `--think`. The system prompt asks for a sentence or two of plain
prose; override it with `--system`.

The model remembers the last `--history-turns` exchanges, so follow-up questions
work as long as you wake her again for each one.

## Talking

```python
import talk

talk.say("Yes?")                      # blocks until the audio finishes
talk.say("Long answer…", blocking=False)  # queued, never talks over the last
talk.stop()                           # cut it off
talk.is_speaking()                    # True while audio is playing
talk.muted_until()                    # inf while speaking, else quiet-since + 0.3s
```

The greeting waits for the noise-floor calibration to finish rather than leading
it — measuring the room while she is talking would set the gate above her own
voice and leave her deaf to the wake word. It names her out loud, which is the
one phrase guaranteed to contain the wake word, and the mute gate is what keeps
her from answering herself.

Piper synthesizes about sixty times faster than real time — a 3.7 s sentence
takes 61 ms — so the reply starts almost immediately. The default voice is
`en_US-ryan-medium` (male, 60 MB, downloaded to `~/.cache/piper` on first use).

Quality tiers are not free. `en_US-ryan-high` sounds richer but synthesizes at
only 9x real time, which puts 0.4 s in front of every acknowledgement — enough to
undo the point of speaking early. Measured on an M5 Pro, per sentence:

| voice | audio | synthesis | ratio |
| --- | --- | --- | --- |
| `en_US-ryan-medium` | 3.68 s | 0.061 s | 60x |
| `en_US-amy-medium` | 4.67 s | 0.090 s | 52x |
| `en_US-ryan-high` | 3.75 s | 0.410 s | 9x |

Only one thing is ever audible at a time: a `blocking=False` reply queued while
another is playing waits its turn instead of talking over it. Anything still
playing at exit is cut short and joined, because letting the interpreter tear
down underneath PortAudio mid-write crashes the process.

```sh
.venv/bin/python -m piper.download_voices                     # list voices
.venv/bin/python -m piper.download_voices en_US-joe-medium \
    --download-dir ~/.cache/piper                             # fetch another
.venv/bin/python talk.py "testing one two three"              # try it
```

## Setup

PyTorch has no wheels for Python 3.14 yet, so the project pins 3.12:

```sh
uv sync            # creates .venv on 3.12 and installs torch, whisper, textual
```

No ffmpeg needed — audio is handed to Whisper as a numpy array rather than a
file, and Piper ships its own espeak-ng data. The first run downloads both
Whisper models (~3.2 GB) to `~/.cache/whisper` and the voice to `~/.cache/piper`.

Ollama is separate — install it, start it, and pull a model:

```sh
brew install ollama && ollama serve &
ollama pull qwen3:14b
```

## Usage

```sh
.venv/bin/python main.py
.venv/bin/python main.py --plain                  # scrolling console output
.venv/bin/python main.py --on-wake 'say yes'      # speak a reply on wake
.venv/bin/python main.py --verbose                # show every wake scan
.venv/bin/python main.py --log events.jsonl       # record wakes and questions
.venv/bin/python main.py --list-devices           # pick a microphone
.venv/bin/python main.py --no-llm                 # transcribe only, no answer
.venv/bin/python main.py --llm-model zero:latest  # answer with another model
.venv/bin/python model.py "why is the sky blue?"  # try the model on its own
```

```
[listening] noise floor 0.0010 rms, gate at 0.0040 rms — say 'ollie' (ctrl-c to stop)

[20:00:00] OLLIE #1 (38 ms)
  ? What is the capital of Portugal?
  … thinking
  > The capital of Portugal is Lisbon. (2.0s)
```

The time in brackets is how long the model itself took; add roughly one
`--window-interval` for the wait before the scan.

### Options

| flag | default | meaning |
| --- | --- | --- |
| `--wake-word` | `ollie` | word to watch for |
| `--wake-model` | `base.en` | fast model that spots the wake word |
| `--model` | `large` | model that transcribes the question |
| `--device` | `auto` | `cpu`, `cuda`, or `mps`; auto prefers the GPU |
| `--language` | `en` | `auto` to let Whisper detect it |
| `--greeting` | `Hi, I'm Ollie, how can I help you?` | spoken once the mic is calibrated (`''` for silence) |
| `--wake-reply` | `Yes?` | spoken the instant the wake word lands (`''` for silence) |
| `--thinking-reply` | 3 phrases | spoken the instant a question lands (`''` for silence) |
| `--voice` | `en_US-ryan-medium` | Piper voice to speak with |
| `--no-voice` | off | never speak, just print |
| `--llm-model` | `qwen3:14b` | Ollama model that answers the question |
| `--llm-host` | `http://localhost:11434` | where Ollama is listening |
| `--system` | see `persona.py` | system prompt handed to the model |
| `--think` | off | let the model reason first — slower, silent while it does |
| `--history-turns` | `6` | past exchanges the model is reminded of |
| `--no-llm` | off | transcribe the question but do not answer it |
| `--on-wake` | none | shell command run the instant the wake word lands |
| `--wake-confidence` | `0.5` | reject hits Whisper scores above this as non-speech |
| `--question-timeout` | `10.0` | give up waiting for a question after this long |
| `--window` | `1.5` | seconds of audio each wake scan looks at |
| `--window-interval` | `0.25` | seconds between wake scans while you speak |
| `--sensitivity` | `3.0` | gate height, as a multiple of the noise floor |
| `--silence` | `0.35` | seconds of quiet that end an utterance |
| `--min-utterance` | `0.25` | ignore shorter blips |
| `--max-utterance` | `15.0` | force a transcription after this much speech |
| `--input-device` | default mic | name or index from `--list-devices` |
| `--log` | off | append wakes and questions to a JSONL file |
| `--plain` | off | console logging instead of the full screen interface |
| `--no-bell` | off | stay silent on a hit |

## Tuning

Measured on an M5 Pro with a 2.2 s clip, seconds per transcription:

| model | cpu | mps | mps + fp16 |
| --- | --- | --- | --- |
| large-v3 | 2.60 | 0.81 | 0.59 |
| turbo | 1.81 | 0.53 | 0.25 |
| small.en | 0.42 | 0.18 | 0.15 |
| base.en | 0.13 | 0.07 | 0.07 |
| tiny.en | 0.07 | 0.06 | 0.05 |

- Wake word missed? Try `--wake-model small.en` (still well under real time),
  or lower `--sensitivity` if the gate is not opening at all.
- False wakes? Raise `--wake-confidence` toward `0.3`, or raise
  `--sensitivity`.
- Questions slow to appear? `--model turbo` is roughly twice as fast as
  `large` for a small accuracy cost.
- On macOS the terminal needs microphone permission
  (System Settings → Privacy & Security → Microphone).
