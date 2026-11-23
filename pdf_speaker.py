#!/usr/bin/env python3
"""A deliberately small PDF reader with reliable TTS play/pause.

Two speech backends are available.  The default sends text to OpenRouter's
hexgrad/kokoro-82m endpoint and plays the returned mp3 with mpv; --backend
speechd keeps the original speech-dispatcher client.  In both cases a spoken
message is one opaque audio stream that cannot be paused mid-sample, so the
reader speaks one sentence at a time and implements pause as "stop the current
chunk, resume it from its beginning".  Predictable beats sample-accurate here.
Sentences are never split merely for length; only unpunctuated runs such as
indexes get broken up by a generous safety ceiling.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

import cairo
import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("Poppler", "0.18")
from gi.repository import Gdk, GLib, Gtk, Poppler

# One sentence per utterance.  The ceiling is only a safety valve for pages
# with no sentence punctuation at all (indexes, code listings); it is high
# enough never to truncate a normal sentence.
MAX_WORDS_PER_CHUNK = 500
DEFAULT_SCALE = 1.4          # pixels per PDF point (~100 dpi)
SENTENCE_END = re.compile(r"[.!?][\"'\u2019\u201d)\]]*$")

# OpenRouter text-to-speech settings.  The key is read from the environment at
# runtime; it is never stored in this repository or in a config file.
OPENROUTER_URL = "https://openrouter.ai/api/v1/audio/speech"
OPENROUTER_MODEL = "hexgrad/kokoro-82m"
OPENROUTER_VOICE = "bf_emma"
AUDIO_CACHE_SIZE = 8          # remembered sentences, for pause/resume and prefetch


# --------------------------------------------------------------------------
# Text model: pdftotext -bbox-layout gives us words *and* their coordinates
# in PDF points (origin top-left), which is exactly what Poppler renders.
# --------------------------------------------------------------------------

@dataclass
class Word:
    index: int               # global index in the reading stream
    page: int
    text: str
    x0: float
    y0: float
    x1: float
    y1: float


@dataclass
class PageWords:
    width: float
    height: float
    words: list


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def rate_is_wired():
    """Whether this speechd setup actually applies the requested rate."""
    if shutil.which("sox"):
        return True                      # the sox tempo branch in piper.conf
    conf = Path.home() / ".config/speech-dispatcher/modules/piper.conf"
    try:
        return "--length-scale" in conf.read_text()
    except OSError:
        return False


def load_words(path):
    xml = subprocess.check_output(
        ["pdftotext", "-bbox-layout", str(path), "-"], text=True
    )
    # pdftotext occasionally emits stray control characters, which are not
    # legal XML 1.0 even though browsers tolerate them.
    xml = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", xml)
    root = ET.fromstring(xml)
    pages, flat = [], []
    for el in root.iter():
        if _local(el.tag) != "page":
            continue
        page_no = len(pages)
        words = []
        for w in el.iter():
            if _local(w.tag) != "word":
                continue
            text = "".join(w.itertext()).strip()
            if not text:
                continue
            words.append(Word(
                index=len(flat) + len(words),
                page=page_no,
                text=text,
                x0=float(w.get("xMin")), y0=float(w.get("yMin")),
                x1=float(w.get("xMax")), y1=float(w.get("yMax")),
            ))
        flat.extend(words)
        pages.append(PageWords(
            width=float(el.get("width")),
            height=float(el.get("height")),
            words=words,
        ))
    return pages, flat


def make_chunks(words, start=0, max_words=MAX_WORDS_PER_CHUNK):
    """Turn a word stream into (start_index, end_index, text) utterances.

    One sentence per utterance.  The word ceiling is only a safety valve for
    pages with no sentence punctuation at all (indexes, code listings).
    """
    chunks, cur, chunk_start = [], [], start
    for i in range(start, len(words)):
        cur.append(i)
        at_end = bool(SENTENCE_END.search(words[i].text))
        if at_end or len(cur) >= max_words:
            chunks.append((chunk_start, i + 1,
                           " ".join(words[j].text for j in cur)))
            cur, chunk_start = [], i + 1
    if cur:
        chunks.append((chunk_start, len(words),
                       " ".join(words[j].text for j in cur)))
    return chunks


# --------------------------------------------------------------------------
# Speech engines.  Both offer the same small interface: speak(text, done,
# failed), cancel(), stop(), prefetch(text, speed), set_rate(multiplier) and
# close().  Engines never touch GTK; completion is reported through callbacks
# that Speaker marshals onto GTK's main loop.
# --------------------------------------------------------------------------

class TTSException(Exception):
    """A speech engine could not produce or play audio."""


class OpenRouterEngine:
    """Fetch mp3 from OpenRouter and play it with mpv.

    HTTP and playback happen in worker threads so the UI never blocks.  Audio
    is cached by (text, speed), which makes resume free and allows the next
    sentence to be fetched while the current one is still playing.
    """

    def __init__(self):
        self.api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
        self.player_bin = shutil.which("mpv")
        self.rate = 1.0
        self.tmpdir = tempfile.mkdtemp(prefix="pdf_speaker_or_")
        self.cache = OrderedDict()
        self.cache_lock = threading.Lock()
        self.pending = set()
        self.generation = 0
        self.player = None
        self.player_lock = threading.Lock()

    def set_rate(self, multiplier):
        self.rate = max(0.25, min(4.0, float(multiplier)))

    def _fetch(self, text, speed):
        if not self.api_key:
            raise TTSException("OPENROUTER_API_KEY is not set")
        if not self.player_bin:
            raise TTSException("mpv is required to play OpenRouter audio")
        body = json.dumps({
            "model": OPENROUTER_MODEL,
            "input": text,
            "voice": OPENROUTER_VOICE,
            "response_format": "mp3",
            "speed": round(speed, 2),
        }).encode("utf-8")
        request = urllib.request.Request(
            OPENROUTER_URL, data=body, method="POST",
            headers={
                "Authorization": "Bearer " + self.api_key,
                "Content-Type": "application/json",
                "Accept": "audio/mpeg",
            },
        )
        for attempt in range(2):
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    data = response.read()
                if not data:
                    raise TTSException("OpenRouter returned no audio")
                return data
            except urllib.error.HTTPError as exc:
                detail = ""
                try:
                    error = json.loads(exc.read().decode("utf-8")).get("error", {})
                    detail = error.get("message", "")
                except Exception:
                    pass
                if exc.code in (429, 502) and attempt == 0:
                    time.sleep(1)
                    continue
                raise TTSException(
                    f"OpenRouter HTTP {exc.code}: {detail or exc.reason}")
            except urllib.error.URLError as exc:
                if attempt == 0:
                    time.sleep(1)
                    continue
                raise TTSException(f"OpenRouter unreachable: {exc.reason}")

    @staticmethod
    def _key(text, speed):
        return text, round(speed, 2)

    def _cached_path(self, key):
        with self.cache_lock:
            if key not in self.cache:
                return None
            self.cache.move_to_end(key)
            return self.cache[key]

    def _store(self, key, data):
        with self.cache_lock:
            if key in self.cache:
                self.cache.move_to_end(key)
                return self.cache[key]
            fd, path = tempfile.mkstemp(suffix=".mp3", dir=self.tmpdir)
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            self.cache[key] = path
            while len(self.cache) > AUDIO_CACHE_SIZE:
                _, old = self.cache.popitem(last=False)
                try:
                    os.unlink(old)
                except OSError:
                    pass
            return path

    def prefetch(self, text, speed):
        key = self._key(text, speed)
        if not self.api_key or self._cached_path(key):
            return
        with self.cache_lock:
            if key in self.pending:
                return
            self.pending.add(key)
        generation = self.generation
        threading.Thread(target=self._prefetch_run,
                         args=(text, speed, key, generation),
                         daemon=True).start()

    def _prefetch_run(self, text, speed, key, generation):
        try:
            data = self._fetch(text, speed)
            if generation == self.generation:
                self._store(key, data)
        except Exception:
            pass                      # prefetch is best-effort
        finally:
            with self.cache_lock:
                self.pending.discard(key)

    def speak(self, text, on_done, on_error):
        speed = self.rate
        key = self._key(text, speed)
        generation = self.generation
        threading.Thread(target=self._speak_run,
                         args=(text, speed, key, generation, on_done, on_error),
                         daemon=True).start()

    def _speak_run(self, text, speed, key, generation, on_done, on_error):
        try:
            path = self._cached_path(key)
            if path is None:
                data = self._fetch(text, speed)
                if generation != self.generation:
                    return
                path = self._store(key, data)
            if generation != self.generation:
                return
            command = [self.player_bin, "--no-video", "--quiet",
                       "--no-terminal", "--force-window=no", path]
            process = subprocess.Popen(command,
                                       stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL)
            with self.player_lock:
                self.player = process
            if generation != self.generation:
                process.terminate()
                return
            status = process.wait()
            with self.player_lock:
                if self.player is process:
                    self.player = None
            if generation == self.generation and status == 0:
                GLib.idle_add(on_done)
        except Exception as exc:
            if generation == self.generation:
                GLib.idle_add(on_error, str(exc))

    def _cancel(self):
        self.generation += 1
        with self.player_lock:
            process = self.player
            self.player = None
        if process is not None and process.poll() is None:
            process.terminate()

    def cancel(self):
        self._cancel()

    def stop(self):
        self._cancel()

    def close(self):
        self._cancel()
        shutil.rmtree(self.tmpdir, ignore_errors=True)


class SpeechdEngine:
    """The original backend: speech-dispatcher over its SSIP socket."""

    def __init__(self):
        import speechd
        self.speechd = speechd
        self.client = speechd.SSIPClient("pdf-speaker")
        self.client.set_priority(speechd.Priority.TEXT)
        self.rate = 1.0

    def set_rate(self, multiplier):
        self.rate = float(multiplier)
        self.client.set_rate(int(round((multiplier - 1.0) * 100)))

    def speak(self, text, on_done, on_error):
        def callback(event_type):
            if event_type == self.speechd.CallbackType.END:
                on_done()
        self.client.speak(
            text,
            callback=callback,
            event_types=(self.speechd.CallbackType.END,
                         self.speechd.CallbackType.CANCEL),
        )

    def cancel(self):
        self.client.cancel()

    def stop(self):
        self.client.stop()

    @staticmethod
    def prefetch(text, speed):
        pass                          # speechd has nothing to prepare

    def close(self):
        try:
            self.client.cancel()
            self.client.close()
        except Exception:
            pass


# --------------------------------------------------------------------------
# Speech controller: chunk bookkeeping, play/pause state and stale-event guard.
# Engine-specific work (HTTP, playback, SSIP) lives in the engines above.
# --------------------------------------------------------------------------

class Speaker:
    def __init__(self, words, on_chunk, on_state, engine, on_error=None):
        self.words = words
        self.on_chunk = on_chunk
        self.on_state = on_state
        self.on_error = on_error or (lambda message: None)
        self.engine = engine
        self.chunks = []
        self.idx = 0
        self.playing = False
        self.epoch = 0                 # stale-callback guard
        self.lock = threading.Lock()

    # -- internal ---------------------------------------------------------
    def _event(self, epoch):
        GLib.idle_add(self._event_main, epoch)

    def _event_main(self, epoch):
        with self.lock:
            if epoch != self.epoch or not self.playing:
                return False
            self.idx += 1
            if self.idx >= len(self.chunks):
                self.playing = False
                done = True
            else:
                done = False
        if done:
            self.on_state(False)
            self.on_chunk(None)
        else:
            self._speak_current()
        return False

    def _error(self, message, epoch):
        with self.lock:
            if epoch != self.epoch:
                return
            self.playing = False
            self.epoch += 1
        self.on_state(False)
        self.on_chunk(None)
        self.on_error(message)

    def _speak_current(self):
        with self.lock:
            if not self.playing or self.idx >= len(self.chunks):
                return
            start, end, text = self.chunks[self.idx]
            epoch = self.epoch
            following = (self.chunks[self.idx + 1][2]
                         if self.idx + 1 < len(self.chunks) else None)
        self.on_chunk((start, end))
        self.engine.speak(text,
                          lambda e=epoch: self._event(e),
                          lambda message, e=epoch: self._error(message, e))
        if following:
            self.engine.prefetch(following, self.engine.rate)

    # -- public -----------------------------------------------------------
    def cue(self, word_index):
        """Point the reader at a word without starting playback."""
        with self.lock:
            self.epoch += 1
            self.playing = False
            self.chunks = make_chunks(self.words, word_index)
            self.idx = 0
        self.engine.cancel()
        self.on_chunk(None)
        self.on_state(False)

    def play_from(self, word_index):
        self.cue(word_index)
        with self.lock:
            self.epoch += 1
            self.playing = True
        self._speak_current()
        self.on_state(True)

    def toggle(self, fallback_word):
        with self.lock:
            playing = self.playing
            have_chunks = bool(self.chunks) and self.idx < len(self.chunks)
        if playing:
            self.pause()
        elif have_chunks:
            with self.lock:
                self.epoch += 1
                self.playing = True
            self._speak_current()
            self.on_state(True)
        else:
            self.play_from(fallback_word)

    def pause(self):
        with self.lock:
            if not self.playing:
                return
            self.playing = False
            self.epoch += 1          # self.idx still points at this chunk
        self.engine.stop()
        self.on_state(False)
        # Keep the interrupted sentence visible: that is where resume will
        # start from.  on_chunk also scrolls its page into view if needed.
        with self.lock:
            current = self.chunks[self.idx][:2] \
                if self.idx < len(self.chunks) else None
        self.on_chunk(current)

    def stop(self):
        with self.lock:
            self.playing = False
            self.epoch += 1
            self.chunks = []
            self.idx = 0
        self.engine.cancel()
        self.on_state(False)
        self.on_chunk(None)

    def set_rate(self, multiplier):
        self.engine.set_rate(multiplier)

    def close(self):
        self.engine.close()


# --------------------------------------------------------------------------
# Xpdf-ish UI: toolbar, one page at a time, click anywhere to set the cursor.
# --------------------------------------------------------------------------

class App:
    def __init__(self, path=None, backend="openrouter"):
        self.backend = backend
        self.doc = None
        self.pages = []
        self.words = []
        self.page_no = 0
        self.scale = DEFAULT_SCALE
        self.cursor_idx = 0
        self.active_range = None
        self.speaker = None
        self.path = None
        self.rate_warned = False
        self._syncing_page = False
        self._page_timer = None

        self.window = Gtk.Window(title="PDF Speaker")
        self.window.set_default_size(900, 780)
        self.window.connect("destroy", self.on_destroy)
        self.window.connect("key-press-event", self.on_key)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.window.add(box)
        box.pack_start(self.build_toolbar(), False, False, 0)

        self.canvas = Gtk.DrawingArea()
        self.canvas.set_events(Gdk.EventMask.BUTTON_PRESS_MASK)
        self.canvas.connect("draw", self.on_draw)
        self.canvas.connect("button-press-event", self.on_click)
        scroll = Gtk.ScrolledWindow()
        scroll.add(self.canvas)
        box.pack_start(scroll, True, True, 0)

        self.status = Gtk.Statusbar()
        box.pack_start(self.status, False, False, 0)

        if path:
            self.open_pdf(path)

    # -- widgets ----------------------------------------------------------
    def build_toolbar(self):
        bar = Gtk.Box(spacing=4)
        bar.set_border_width(4)

        def button(label, fn, tip=None):
            b = Gtk.Button(label=label)
            b.connect("clicked", fn)
            if tip:
                b.set_tooltip_text(tip)
            bar.pack_start(b, False, False, 0)
            return b

        button("_Open", lambda b: self.choose_file(), "Ctrl+O")
        bar.pack_start(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL),
                       False, False, 4)
        self.play_btn = button("▶ _Play", lambda b: self.toggle_play(),
                               "Space")
        button("■ _Stop", lambda b: self.stop(), "S")
        bar.pack_start(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL),
                       False, False, 4)
        button("◀ Page", lambda b: self.jump_to_page(self.page_no - 1),
               "Left")
        button("Page ▶", lambda b: self.jump_to_page(self.page_no + 1),
               "Right")
        self.page_spin = Gtk.SpinButton.new_with_range(1, 1, 1)
        self.page_spin.set_numeric(True)
        self.page_spin.set_digits(0)
        self.page_spin.set_update_policy(Gtk.SpinButtonUpdatePolicy.IF_VALID)
        self.page_spin.set_width_chars(5)
        self.page_spin.set_tooltip_text("Page number — type one and press Enter")
        self.page_spin.connect("value-changed", self.on_page_spin)
        self.page_spin.connect("activate", self.on_page_activate)
        bar.pack_start(self.page_spin, False, False, 0)
        self.page_total = Gtk.Label(label="/ –")
        bar.pack_start(self.page_total, False, False, 4)
        bar.pack_start(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL),
                       False, False, 4)
        button("−", lambda b: self.zoom(1 / 1.2), "Zoom out")
        button("+", lambda b: self.zoom(1.2), "Zoom in")

        bar.pack_start(Gtk.Label(label="Speed"), False, False, 4)
        self.rate = Gtk.Scale.new_with_range(
            Gtk.Orientation.HORIZONTAL, 0.5, 2.0, 0.05)
        self.rate.set_value(1.0)
        self.rate.set_digits(2)
        self.rate.set_value_pos(Gtk.PositionType.RIGHT)
        self.rate.set_size_request(160, -1)
        self.rate.connect("format-value",
                          lambda scale, value: f"{value:.2f}×")
        self.rate.connect("value-changed", self.on_rate)
        bar.pack_start(self.rate, False, False, 0)
        return bar

    # -- document ---------------------------------------------------------
    def choose_file(self):
        dialog = Gtk.FileChooserDialog(
            "Open PDF", self.window, Gtk.FileChooserAction.OPEN,
            (Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
             Gtk.STOCK_OPEN, Gtk.ResponseType.OK))
        flt = Gtk.FileFilter()
        flt.set_name("PDF files")
        flt.add_mime_type("application/pdf")
        dialog.add_filter(flt)
        if dialog.run() == Gtk.ResponseType.OK:
            self.open_pdf(dialog.get_filename())
        dialog.destroy()

    def open_pdf(self, path):
        try:
            doc = Poppler.Document.new_from_file(Path(path).as_uri(), None)
            pages, words = load_words(path)
        except Exception as exc:
            self.error("Could not open PDF", str(exc))
            return
        if self.speaker:
            self.speaker.close()
        self.doc, self.pages, self.words = doc, pages, words
        self.page_spin.get_adjustment().set_upper(doc.get_n_pages())
        self.path = path
        self.cursor_idx = 0
        self.active_range = None
        try:
            if self.backend == "openrouter":
                engine = OpenRouterEngine()
            else:
                engine = SpeechdEngine()
            self.speaker = Speaker(words, self.on_chunk, self.on_state,
                                   engine, self.on_speech_error)
        except Exception as exc:
            self.speaker = None
            name = "OpenRouter" if self.backend == "openrouter" \
                else "Speech Dispatcher"
            self.error(f"{name} unavailable", str(exc))
        else:
            self.apply_rate()
        self.window.set_title("PDF Speaker – " + path.rsplit("/", 1)[-1])
        self.set_page(0)
        self.update_status()

    def set_page(self, n, sync_spin=True):
        if not self.doc or not (0 <= n < self.doc.get_n_pages()):
            return
        self.page_no = n
        page = self.doc.get_page(n)
        w, h = page.get_size()
        self.canvas.set_size_request(int(w * self.scale), int(h * self.scale))
        if sync_spin:
            self._syncing_page = True
            self.page_spin.set_value(n + 1)
            self._syncing_page = False
        self.page_total.set_text(f"/ {self.doc.get_n_pages()}")
        self.canvas.queue_draw()
        self.update_status()

    def first_word_on_page(self, n):
        """Index of the first word on or after page n (skips empty pages)."""
        for word in self.words:
            if word.page >= n:
                return word.index
        return None

    def jump_to_page(self, n):
        """Move the view and the reading cursor to page n."""
        if not self.doc or not (0 <= n < self.doc.get_n_pages()):
            return
        was_playing = bool(self.speaker and self.speaker.playing)
        self.set_page(n)
        if not self.speaker:
            return
        start = self.first_word_on_page(n)
        if start is None:
            self.speaker.stop()
            return
        self.cursor_idx = start
        if was_playing:
            self.speaker.play_from(start)
        else:
            self.speaker.cue(start)
        self.canvas.queue_draw()
        self.update_status()

    def on_page_spin(self, spin):
        # Debounce: typing "200" must not jump to page 2 and then 20.
        if self._syncing_page or not self.doc:
            return
        if self._page_timer is not None:
            GLib.source_remove(self._page_timer)
        self._page_timer = GLib.timeout_add(300, self.commit_page_spin)

    def on_page_activate(self, spin):
        if self._page_timer is not None:
            GLib.source_remove(self._page_timer)
            self._page_timer = None
        self.commit_page_spin()

    def commit_page_spin(self):
        self._page_timer = None
        if not self.doc:
            return False
        # On Enter, GTK emits `activate` before it parses the entry text into
        # the adjustment, so get_value() is still the old page.  Read the text
        # directly and push the parsed value back before jumping.
        text = self.page_spin.get_text().strip()
        try:
            number = int(float(text))
        except ValueError:
            number = int(self.page_spin.get_value())
        number = max(1, min(self.doc.get_n_pages(), number))
        self._syncing_page = True
        self.page_spin.set_value(number)
        self._syncing_page = False
        self.jump_to_page(number - 1)
        return False

    def zoom(self, factor):
        self.scale = max(0.5, min(4.0, self.scale * factor))
        self.set_page(self.page_no)

    # -- drawing / input --------------------------------------------------
    def on_draw(self, widget, cr):
        if not self.doc:
            return False
        cr.save()
        cr.scale(self.scale, self.scale)
        page = self.doc.get_page(self.page_no)
        page.render(cr)

        if self.active_range:
            start, end = self.active_range
            cr.set_source_rgba(1.0, 0.84, 0.2, 0.30)
            for word in self.words[start:end]:
                if word.page == self.page_no:
                    cr.rectangle(word.x0, word.y0,
                                 word.x1 - word.x0, word.y1 - word.y0)
                    cr.fill()

        if 0 <= self.cursor_idx < len(self.words):
            word = self.words[self.cursor_idx]
            if word.page == self.page_no:
                cr.set_source_rgba(0.1, 0.45, 0.95, 0.95)
                cr.set_line_width(1.5 / self.scale)
                cr.rectangle(word.x0 - 2, word.y0 - 2,
                             word.x1 - word.x0 + 4, word.y1 - word.y0 + 4)
                cr.stroke()
        cr.restore()
        return False

    def on_click(self, widget, event):
        if not self.pages:
            return
        x, y = event.x / self.scale, event.y / self.scale
        best, best_dist = None, float("inf")
        for word in self.pages[self.page_no].words:
            dx = max(word.x0 - x, 0, x - word.x1)
            dy = max(word.y0 - y, 0, y - word.y1)
            dist = dx * dx + dy * dy
            if dist < best_dist:
                best, best_dist = word, dist
        if best is None:
            return
        was_playing = bool(self.speaker and self.speaker.playing)
        self.cursor_idx = best.index
        if self.speaker:
            if was_playing:
                self.speaker.play_from(best.index)   # jump while listening
            else:
                self.speaker.cue(best.index)
        self.canvas.queue_draw()
        self.update_status()

    # -- playback ---------------------------------------------------------
    def toggle_play(self):
        if self.speaker:
            self.speaker.toggle(self.cursor_idx)

    def stop(self):
        if self.speaker:
            self.speaker.stop()

    def on_chunk(self, chunk):
        self.active_range = chunk
        if chunk and self.words:
            page = self.words[chunk[0]].page
            if page != self.page_no:
                self.set_page(page)
        self.canvas.queue_draw()
        self.update_status()

    def on_state(self, playing):
        self.play_btn.set_label("⏸ _Pause" if playing else "▶ _Play")
        self.update_status()

    def apply_rate(self):
        """Pass the 0.5–2.0 multiplier to the active engine."""
        if self.speaker:
            self.speaker.set_rate(self.rate.get_value())

    def on_rate(self, scale):
        self.apply_rate()
        if self.backend == "speechd" and not rate_is_wired() \
                and not self.rate_warned:
            self.rate_warned = True
            self.status.push(1, "Speed needs sox, or --length-scale $RATE "
                                "in piper.conf — see README")

    def on_speech_error(self, message):
        self.status.push(1, "Speech error: " + message.splitlines()[0][:180])

    # -- misc -------------------------------------------------------------
    def update_status(self):
        if not self.words:
            self.status.push(1, "Open a PDF, click a word, press Space to read")
            return
        state = "speaking" if self.speaker and self.speaker.playing else "paused"
        where = self.active_range[0] if self.active_range else self.cursor_idx
        self.status.push(1, f"{state} · word {where + 1}/{len(self.words)} · "
                            "click a word to set the reading cursor")

    def on_key(self, widget, event):
        name = Gdk.keyval_name(event.keyval)
        if event.state & Gdk.ModifierType.CONTROL_MASK and name == "o":
            self.choose_file()
        elif self.page_spin.has_focus():
            # Let the page entry handle typing, arrows and Enter itself.
            return False
        elif name == "space":
            self.toggle_play()
        elif name in ("s", "S"):
            self.stop()
        elif name == "Left":
            self.jump_to_page(self.page_no - 1)
        elif name == "Right":
            self.jump_to_page(self.page_no + 1)
        else:
            return False
        return True

    def on_destroy(self, widget):
        if self.speaker:
            self.speaker.close()
        Gtk.main_quit()

    def error(self, title, message):
        dialog = Gtk.MessageDialog(self.window, 0, Gtk.MessageType.ERROR,
                                   Gtk.ButtonsType.OK, title)
        dialog.format_secondary_text(message)
        dialog.run()
        dialog.destroy()

    def run(self):
        self.window.show_all()
        if self.backend == "openrouter" and not os.environ.get("OPENROUTER_API_KEY"):
            self.status.push(1, "Set OPENROUTER_API_KEY to enable speech")
        Gtk.main()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", nargs="?", help="PDF file to open")
    parser.add_argument("--backend", choices=("openrouter", "speechd"),
                        default="openrouter",
                        help="TTS backend (default: openrouter)")
    args = parser.parse_args()
    App(args.pdf, args.backend).run()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
