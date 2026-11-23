# pdf_speaker — design sketch

A deliberately small, Xpdf-like PDF reader whose only extra trick is reliable
text-to-speech with play/pause and "read from here".

Made with [OpenCode](https://opencode.ai/) using the **Big Pickle** model.

Repository: https://github.com/David-Chatterton/pdf_speaker

This branch adds an OpenRouter speech backend. The original speech-dispatcher
backend remains available with `--backend speechd`.

## Run

```sh
export OPENROUTER_API_KEY="..."          # from https://openrouter.ai/settings/keys
python3 pdf_speaker.py [some.pdf]
```

The key is read from the environment at runtime. It is never written to a file
in this repository. If it is missing, the PDF still opens and a status message
explains what to set.

Original backend:

```sh
python3 pdf_speaker.py --backend speechd [some.pdf]
```

### Dark desktop themes

PDF pages are rendered with a transparent background. Under a dark GTK theme,
the dark window background can show through, leaving black text on a black
page. The simplest workaround is to launch the application with a light GTK
theme:

```sh
GTK_THEME=Adwaita:light python3 pdf_speaker.py some.pdf
```

This is useful on Plasma desktops using Breeze Dark. No code change is needed.

## OpenRouter backend

Text is sent to OpenRouter's text-to-speech endpoint:

- Model: `hexgrad/kokoro-82m`
- Voice: `bf_emma` (British female)
- Endpoint: `POST https://openrouter.ai/api/v1/audio/speech`
- Response: raw mp3 bytes, played by `mpv`
- Request fields: `model`, `input`, `voice`, `response_format: "mp3"`, `speed`

The model identifier, voice and response format are constants near the top of
`pdf_speaker.py`. `kokoro.md` in this branch is the API reference used during
development; it lists the other supported voices and the error codes.

Audio is fetched in a worker thread so the UI never blocks. Each sentence is
cached on disk by `(text, speed)`, which has two benefits: pressing pause and
then play replays the cached audio rather than paying for a second generation,
and the next sentence is fetched in the background while the current one plays
to hide network latency. The cache holds eight entries; older files are deleted
automatically. Temporary files are removed when the application closes.

Synthesis is billed by OpenRouter per character. At the time of writing,
DeepInfra charges about $0.99 per million characters and Together about $4 per
million. A sentimental audiobook is therefore inexpensive, but a long book with
repeated rewinds is not free. Failed generations are not billed.

## Dependencies

All dependencies are provided by the distribution; there is nothing to
`pip install`. Only the OpenRouter backend needs an account and an API key.

### Always required

| Requirement | Used for | Arch / CachyOS | Debian / Ubuntu |
| --- | --- | --- | --- |
| Python 3.8+ | application runtime | `python` | `python3` |
| GTK 3 + PyGObject | UI (`Gtk`, `Gdk`, `GLib`) | `gtk3`, `python-gobject` | `gir1.2-gtk-3.0`, `python3-gi` |
| Poppler GLib typelib | page rendering | `poppler-glib` | `gir1.2-poppler-0.18` |
| pycairo | drawing the page and highlight | `python-cairo` | `python3-cairo` |
| Poppler utils | word text + boxes via `pdftotext -bbox-layout` | `poppler` | `poppler-utils` |

The HTTP client uses Python's standard library (`urllib.request`), so no extra
Python package is needed.

### OpenRouter backend (default)

| Requirement | Used for | Arch / CachyOS | Debian / Ubuntu |
| --- | --- | --- | --- |
| `mpv` | playing the returned mp3 | `mpv` | `mpv` |
| OpenRouter API key | synthesis | environment variable `OPENROUTER_API_KEY` | same |

### speechd backend (`--backend speechd`)

| Requirement | Used for | Arch / CachyOS | Debian / Ubuntu |
| --- | --- | --- | --- |
| Python speechd bindings | SSIP client | `speech-dispatcher` | `python3-speechd` |
| speech-dispatcher daemon with a working module | synthesis | `speech-dispatcher` + synthesizer | `speech-dispatcher` + synthesizer |
| `sox` | speed slider (`tempo $RATE` in the module config) | `sox` | `sox` |

On Arch/CachyOS, the minimal OpenRouter installation is:

```sh
sudo pacman -S gtk3 python-gobject poppler-glib python-cairo poppler mpv
```

The speech-dispatcher synthesizer executable and voice models are installed
separately (the original setup used Pied via Flatpak). Any working
speech-dispatcher output module will do; piper is simply what this project was
developed against. Voice model choice affects CPU load substantially: in
testing, `en_GB-cori-medium` was noticeably cheaper than `en_GB-alan-medium`.

## Controls

| Action | Control |
| --- | --- |
| Open PDF | `Ctrl+O` / Open |
| Play / pause | `Space` / Play button |
| Stop | `S` / Stop button |
| Start reading from a word | click it |
| Jump while listening | click another word |
| Jump to a page | type its number in the toolbar and press Enter |
| Pages | `←` / `→`, prev/next buttons, or the page spinner |
| Zoom / speed | toolbar |

Clicking sets a blue outline on the word. The shaded yellow region is the
sentence currently being spoken. The view follows it onto the next page.

Any page change — typed number, spinner arrows, buttons or arrow keys — also
moves the reading cursor to the first extractable word on that page. If
playback is active it continues from there; otherwise the reader stays paused
and Play resumes from the new page. This prevents playback auto-follow from
immediately undoing a manual jump. Pages without extractable text are skipped
to the next page that has it.

## Architecture

```
pdftotext -bbox-layout  ->  word stream (text + PDF coordinates)
                                   |
                   +---------------+---------------+
                   |                               |
          GTK DrawingArea                      Speaker
          Poppler renders page              utterance chunks
          hit-test click -> word            epoch guard, play/pause
                   |                               |
                   +-------- active range ---------+
                                                   |
                                       +-----------+-----------+
                                       |                       |
                                OpenRouterEngine          SpeechdEngine
                                HTTP -> mp3 cache          SSIP socket
                                mpv playback               END/CANCEL
```

* `load_words()` parses `pdftotext -bbox-layout` XHTML. Its `<word>`
  elements carry both the text and a bounding box in PDF points with a
  top-left origin — the same coordinate space Poppler renders into. That
  makes click-to-word a simple nearest-box search, with no OCR or fuzzy text
  matching.
* `make_chunks()` groups the flat word stream into utterances of one sentence
  each. A generous `MAX_WORDS_PER_CHUNK = 500` ceiling only breaks up pages
  with no sentence punctuation at all (indexes, code listings). Chunks may
  cross pages; the highlight and auto page change follow the word indices.
* `Speaker` keeps the state machine (current chunk, playing/paused) and an
  `epoch` counter. Engines report completion or failure through callbacks,
  which are marshalled to GTK with `GLib.idle_add`. Stale events from a stopped
  or re-cued utterance are discarded by epoch, so callbacks never have to
  issue commands back into the engine.

## Why pause is implemented as stop + re-speak

Neither backend exposes a usable "pause this audio sample" operation. An
OpenRouter response is a complete mp3 file, and with the original `sd_generic`
piper setup a speech message becomes one opaque stream that speech-dispatcher
cannot interrupt. The reader therefore speaks one chunk at a time:

* **pause** — stop the current audio, remember its chunk index;
* **resume** — replay that chunk from its beginning;
* **stop** — cancel and clear the queue.

Resume always works. With the OpenRouter backend the replay comes from the
local cache, so it costs nothing extra; with speech-dispatcher the sentence is
synthesised again.

### Gaps between sentences

With OpenRouter, each utterance has network latency. The reader hides most of
it by prefetching the next sentence's audio while the current one plays. A
small gap can still occur if the prefetch has not finished in time, or if the
cache was cleared by a rate change.

With speech-dispatcher, piper loads its voice for every utterance when invoked
as a separate process; a measured load time was around 0.63 s with the original
voice. A lighter voice model reduces this substantially.

Each utterance is exactly one sentence, so no sentence is cut short. An early
version grouped two sentences per utterance with a 45-word ceiling, but that
ceiling truncated longer second sentences. The current version drops the
grouping and raises the ceiling to 500 words — high enough never to split a
normal sentence, but still a safety valve for unpunctuated runs such as indexes.
`MAX_WORDS_PER_CHUNK` at the top of `pdf_speaker.py` controls that ceiling.

## Speed slider

The slider is a playback multiplier, 0.5×–2× in both backends.

With OpenRouter, the multiplier is sent directly as the API's `speed` field.
The model accepts 0.25–4, so the slider's range is always valid and the rate
change takes effect on the next generated sentence.

With speech-dispatcher, the multiplier maps onto a module configured with
`GenericRateAdd 1` and `GenericRateMultiply 1`:

```
$RATE = (ssip_rate * GenericRateMultiply) + GenericRateAdd
      = (ssip_rate * 0.01) + 1
```

(`GenericRateMultiply 1` means 0.01 — multiply values are written ×100 in
`speechd.conf`.) So SSIP rate 0 is 1.0×, +100 is 2.0×, −50 is 0.5×. A piper
module that checks `command -v sox` falls back to `PROCESS='cat'` when sox is
absent, and `$RATE` is then discarded. Installing sox activates the existing
`sox ... tempo $RATE` branch, or `--length-scale $RATE` can be added to the
piper invocation instead. Either change affects *all* speech-dispatcher
clients, not just this reader. The app shows a one-time hint in the status bar
when the speechd backend is selected and neither fix is in place.

## Deliberately left out

* Word-level karaoke highlighting — neither backend supplies word timings, so
  the current sentence is shaded rather than faking a position inside it.
* Bookmarks, saved position, search, annotations, reflow, multi-column
  heuristics, encrypted PDFs, continuous scrolling.
* Voice selection UI — the OpenRouter voice is a constant in `pdf_speaker.py`;
  the speechd backend uses the voice configured in speech-dispatcher.

## Possible next steps

1. Route audio through an mpv IPC socket instead of a fresh process per
   sentence. This would remove the player startup cost and allow exact pause,
   but adds a protocol and lifecycle to maintain.
2. Use `Poppler.Page.get_text_layout()` rectangles to avoid the `pdftotext`
   subprocess, pairing them with `get_text()` tokens (needs care with
   hyphenation and ligatures).
3. Persist the last reading position per file so restarting the app resumes.
4. Add a voice picker backed by the `voice` enum in the model discovery
   response, instead of editing a constant.
