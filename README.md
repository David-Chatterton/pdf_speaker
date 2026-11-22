# pdf_speaker — design sketch

A deliberately small, Xpdf-like PDF reader whose only extra trick is reliable
text-to-speech with play/pause and "read from here".

Made with [OpenCode](https://opencode.ai/) using the **Big Pickle** model.

Repository: https://github.com/David-Chatterton/pdf_speaker

## Run

```sh
python3 pdf_speaker.py [some.pdf]
```

## Dependencies

Everything comes from the distro — there is nothing to `pip install`. The app
is a speech-dispatcher client: it connects to the running daemon over its SSIP
socket and sends it text. The piper voice and its model are configured in
speech-dispatcher, **not** in this repository.

| Requirement | Used for | Arch / CachyOS | Debian / Ubuntu |
| --- | --- | --- | --- |
| Python 3.8+ | application runtime | `python` | `python3` |
| GTK 3 + PyGObject | UI (`Gtk`, `Gdk`, `GLib`) | `gtk3`, `python-gobject` | `gir1.2-gtk-3.0`, `python3-gi` |
| Poppler GLib typelib | page rendering | `poppler-glib` | `gir1.2-poppler-0.18` |
| pycairo | drawing the page and highlight | `python-cairo` | `python3-cairo` |
| Poppler utils | word text + boxes via `pdftotext -bbox-layout` | `poppler` | `poppler-utils` |
| Python speechd bindings | SSIP client | `speech-dispatcher` | `python3-speechd` |
| speech-dispatcher daemon with a working module | synthesis | `speech-dispatcher` + piper | `speech-dispatcher` + piper |
| `sox` | speed slider (`tempo $RATE` in piper.conf) | `sox` | `sox` |

On Arch/CachyOS:

```sh
sudo pacman -S gtk3 python-gobject poppler-glib python-cairo poppler speech-dispatcher sox
```

The piper executable and voice models are installed separately (here they are
managed by Pied via Flatpak). Any working speech-dispatcher output module will
do; piper is simply what this setup uses. Voice model choice also matters for
CPU load — `en_GB-cori-medium` is noticeably cheaper here than
`en_GB-alan-medium`.

## Controls

| Action | Control |
| --- | --- |
| Open PDF | `Ctrl+O` / Open |
| Play / pause | `Space` / Play button |
| Stop | `S` / Stop button |
| Start reading from a word | click it |
| Jump while listening | click another word |
| Pages | `←` / `→` |
| Zoom / speed | toolbar |

Clicking sets a blue outline on the word. The shaded yellow region is the
utterance currently being spoken (now up to two sentences). The view follows
it onto the next page.

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
* `make_chunks()` groups the flat word stream into utterances of up to
  `SENTENCES_PER_CHUNK` sentences and `MAX_WORDS_PER_CHUNK` words. Chunks may
  cross pages; the highlight and auto page change follow the word indices.
* `Speaker` keeps the state machine (current chunk, playing/paused) and an
  `epoch` counter. `speechd` fires END/CANCEL on its own thread; those events
  are marshalled to GTK with `GLib.idle_add`, and stale events from a stopped
  or re-cued utterance are discarded by epoch. Callbacks never issue SSIP
  commands (that would deadlock the client library).

## Why pause is implemented as stop + re-speak

Your `~/.config/speech-dispatcher/modules/piper.conf` uses `sd_generic` to run
piper and pipe raw audio straight into `paplay`. The whole message becomes one
opaque audio stream that speech-dispatcher has no control over once it starts
— its SSIP `PAUSE`/`RESUME` commands cannot stop those samples. Okular's flaky
resume comes from the same place.

So instead of pretending we have sample-accurate pause, we speak one chunk at
a time:

* **pause** — `STOP` the current utterance, remember its chunk index;
* **resume** — re-speak that chunk from its beginning;
* **stop** — `CANCEL` and clear the queue.

Resume always works; the cost is at most one repeated chunk.

### The between-sentence gap, and what was changed

Your piper log shows `Loaded voice in ~0.63 second(s)` on **every** utterance.
That is the pause you hear — piper is a fresh process per speech message.

The low-risk fix, now applied, is to group two sentences into each utterance
(`SENTENCES_PER_CHUNK = 2`) while keeping the original 45-word ceiling. On the
test PDF that cuts the number of voice loads by about 31% (941 → 652
utterances) with **no increase in worst-case pause rewind** — 45 words either
way. The average utterance grows from 23 to 33 words, so a pause re-speaks a
bit more text on average. Both are one-line constants at the top of
`pdf_speaker.py`; set `SENTENCES_PER_CHUNK = 1` if you want the old behaviour,
or lower `MAX_WORDS_PER_CHUNK` if 45 words feels like too much to re-hear.

Voice model choice matters more than any of this: switching from
`en_GB-alan-medium` to `en_GB-cori-medium` reduced CPU load here far more than
chunk tuning did.

I would **not** replace the audio pipeline just to remove the gap. Doing so
( piper → WAV → mpv IPC ) would give gapless playback and exact pause, but it
duplicates your voice configuration, adds a second playback stack and a new
set of failure modes. Not worth it unless the grouped version still bothers
you — see Possible next steps.

## Speed slider

The slider is a playback multiplier, 0.5×–2×. This maps exactly onto your
speech-dispatcher configuration:

```
$RATE = (ssip_rate * GenericRateMultiply) + GenericRateAdd
      = (ssip_rate * 0.01) + 1
```

(`GenericRateMultiply 1` means 0.01 — the multiply values are written ×100 in
speechd.conf.) So SSIP rate 0 is 1.0×, +100 is 2.0×, −50 is 0.5×. The app
converts the displayed multiplier back to SSIP rate.

**Why it did nothing at first:** `sox` was not installed. piper.conf tests
`command -v sox`; when it is missing, the pipeline falls back to `PROCESS='cat'`
and `$RATE` is simply discarded. The SSIP command was being sent correctly all
along. Installing `sox` (`sudo pacman -S sox` on Arch/CachyOS) activates the
existing `sox ... tempo $RATE` branch.

The alternative, if you would rather avoid the extra package, is to add
`--length-scale $RATE` to the piper invocation in `piper.conf` — piper's own
rate control. Note that either change affects *all* speech-dispatcher clients,
not just this reader. The app shows a one-time hint in the status bar if it
detects that neither fix is in place.

## Deliberately left out

* Word-level karaoke highlighting — SSIP gives no word timings, so we shade
  the current utterance rather than fake a position inside it.
* Bookmarks, saved position, search, annotations, reflow, multi-column
  heuristics, encrypted PDFs, continuous scrolling.
* Voice selection UI — set it in speech-dispatcher as you already have.

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
