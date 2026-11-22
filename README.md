# pdf_speaker — design sketch

A deliberately small, Xpdf-like PDF reader whose only extra trick is reliable
text-to-speech with play/pause and "read from here".

## Run

```sh
python3 pdf_speaker.py [some.pdf]
```

No pip installs: GTK 3, Poppler Gi, `pdftotext` and the Python `speechd`
bindings are already on this machine. It uses your existing speech-dispatcher
piper configuration via the SSIP socket — no separate piper setup.

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

**Why it did nothing before:** `sox` is not installed on this laptop. Your
piper.conf tests `command -v sox`; when it is missing, the pipeline falls back
to `PROCESS='cat'` and `$RATE` is simply discarded. The SSIP command was being
sent correctly all along.

Two ways to fix it (neither touches this app):

1. Install sox: `sudo apt install sox` (or your distro equivalent). The
   existing `sox ... tempo $RATE` branch then activates.
2. Or add `--length-scale $RATE` to the piper invocation in
   `~/.config/speech-dispatcher/modules/piper.conf`, which uses piper's own
   rate control and needs no extra package. Note this changes speed for
   *all* speech-dispatcher clients, not just this reader.

Until one of those is done, moving the slider shows a one-time hint in the
status bar.

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
