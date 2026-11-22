# pdf_speaker — design sketch

A deliberately small, Xpdf-like PDF reader whose only extra trick is reliable
text-to-speech with play/pause and "read from here".

Made with [OpenCode](https://opencode.ai/) using the **Big Pickle** model.

Repository: https://github.com/David-Chatterton/pdf_speaker

## Run

```sh
python3 pdf_speaker.py [some.pdf]
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

## Dependencies

All dependencies are provided by the distribution; there is nothing to
`pip install`. The application is a speech-dispatcher client: it connects to
the running daemon over its SSIP socket and sends text to it. The synthesizer
and its voices are configured in speech-dispatcher, **not** in this repository.

| Requirement | Used for | Arch / CachyOS | Debian / Ubuntu |
| --- | --- | --- | --- |
| Python 3.8+ | application runtime | `python` | `python3` |
| GTK 3 + PyGObject | UI (`Gtk`, `Gdk`, `GLib`) | `gtk3`, `python-gobject` | `gir1.2-gtk-3.0`, `python3-gi` |
| Poppler GLib typelib | page rendering | `poppler-glib` | `gir1.2-poppler-0.18` |
| pycairo | drawing the page and highlight | `python-cairo` | `python3-cairo` |
| Poppler utils | word text + boxes via `pdftotext -bbox-layout` | `poppler` | `poppler-utils` |
| Python speechd bindings | SSIP client | `speech-dispatcher` | `python3-speechd` |
| speech-dispatcher daemon with a working module | synthesis | `speech-dispatcher` + synthesizer | `speech-dispatcher` + synthesizer |
| `sox` | speed slider (`tempo $RATE` in the module config) | `sox` | `sox` |

On Arch/CachyOS:

```sh
sudo pacman -S gtk3 python-gobject poppler-glib python-cairo poppler speech-dispatcher sox
```

The synthesizer executable and voice models are installed separately (the
original setup uses Pied via Flatpak). Any working speech-dispatcher output
module will do; piper is simply what this project was developed against.
Voice model choice affects CPU load substantially: in testing,
`en_GB-cori-medium` was noticeably cheaper than `en_GB-alan-medium`.

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
          GTK DrawingArea                  Speaker (SSIP)
          Poppler renders page              utterance chunks
          hit-test click -> word            END/CANCEL callback
                   |                               |
                   +-------- active range ---------+
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
  `epoch` counter. `speechd` fires END/CANCEL on its own thread; those events
  are marshalled to GTK with `GLib.idle_add`, and stale events from a stopped
  or re-cued utterance are discarded by epoch. Callbacks never issue SSIP
  commands (that would deadlock the client library).

## Why pause is implemented as stop + re-speak

A typical piper setup uses `sd_generic` to run piper and pipe raw audio
straight into `paplay`. The whole message becomes one opaque audio stream that
speech-dispatcher has no control over once it starts — its SSIP `PAUSE` and
`RESUME` commands cannot stop those samples. This is also why applications
such as Okular can behave unreliably when resuming with such a module.

Instead of pretending to have sample-accurate pause, the reader speaks one
chunk at a time:

* **pause** — `STOP` the current utterance, remember its chunk index;
* **resume** — re-speak that chunk from its beginning;
* **stop** — `CANCEL` and clear the queue.

Resume always works; the cost is at most one repeated chunk.

### The between-utterance gap

Piper loads its voice for every utterance when it is invoked as a separate
process; a measured load time was around 0.63 s with the original voice. That
is the source of the pause between sentences. A lighter voice model reduces
this substantially and makes one-sentence utterances practical.

Each utterance is exactly one sentence, so no sentence is cut short. An early
version grouped two sentences per utterance with a 45-word ceiling, but that
ceiling truncated longer second sentences. The current version drops the
grouping and raises the ceiling to 500 words — high enough never to split a
normal sentence, but still a safety valve for unpunctuated runs such as indexes.
`MAX_WORDS_PER_CHUNK` at the top of `pdf_speaker.py` controls that ceiling.

Replacing the audio pipeline (piper → WAV → mpv IPC) would give gapless
playback and exact pause, but it duplicates the voice configuration, adds a
second playback stack and introduces new failure modes. It is not worth doing
solely to remove the gap — see Possible next steps.

## Speed slider

The slider is a playback multiplier, 0.5×–2×. This maps exactly onto a
speech-dispatcher module configured with `GenericRateAdd 1` and
`GenericRateMultiply 1`:

```
$RATE = (ssip_rate * GenericRateMultiply) + GenericRateAdd
      = (ssip_rate * 0.01) + 1
```

(`GenericRateMultiply 1` means 0.01 — multiply values are written ×100 in
`speechd.conf`.) So SSIP rate 0 is 1.0×, +100 is 2.0×, −50 is 0.5×. The app
converts the displayed multiplier back to SSIP rate.

The speed slider needs the synthesizer module to apply `$RATE`. A piper module
that checks `command -v sox` falls back to `PROCESS='cat'` when sox is absent,
and `$RATE` is then discarded. Installing sox activates the existing
`sox ... tempo $RATE` branch:

```sh
sudo pacman -S sox    # Arch/CachyOS
sudo apt install sox  # Debian/Ubuntu
```

Alternatively, add `--length-scale $RATE` to the piper invocation in
`speechd.conf`'s module configuration — piper's own rate control, with no
extra package. Either change affects *all* speech-dispatcher clients, not just
this reader. The app shows a one-time hint in the status bar if it detects
that neither fix is in place.

## Deliberately left out

* Word-level karaoke highlighting — SSIP gives no word timings, so the current
  utterance is shaded rather than faking a position inside it.
* Bookmarks, saved position, search, annotations, reflow, multi-column
  heuristics, encrypted PDFs, continuous scrolling.
* Voice selection UI — configure the voice in speech-dispatcher.

## Possible next steps

1. If the remaining gaps are still audible: pre-render utterances to WAV with
   piper directly and play them through an mpv IPC socket. This gives gapless
   playback, exact pause/resume and word timings, and rate control via piper's
   `--length-scale` — at the cost of a second playback stack and duplicating
   voice config.
2. Use `Poppler.Page.get_text_layout()` rectangles to avoid the `pdftotext`
   subprocess, pairing them with `get_text()` tokens (needs care with
   hyphenation and ligatures).
3. Persist the last reading position per file so restarting the app resumes.
