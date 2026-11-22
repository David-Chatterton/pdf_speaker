#!/usr/bin/env python3
"""A deliberately small PDF reader with reliable TTS play/pause.

Design constraint: with the sd_generic -> piper -> paplay module, a speech
message is one opaque audio stream.  speech-dispatcher cannot pause it.
So we speak short groups of sentences one at a time and implement pause as
"stop the current chunk, resume it from its beginning".  Predictable beats
sample-accurate here.  Grouping two sentences per utterance halves the
number of piper restarts (each costs ~0.6 s of voice-load time).
"""

import argparse
import re
import shutil
import subprocess
import sys
import threading
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

import cairo
import gi
import speechd
from speechd import CallbackType

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("Poppler", "0.18")
from gi.repository import Gdk, GLib, Gtk, Poppler

# Each utterance costs ~0.6 s to load the piper voice.  Two sentences per
# utterance cuts the number of loads by ~30% while keeping the worst-case
# pause rewind at the same 45-word ceiling as a single sentence.
SENTENCES_PER_CHUNK = 2
MAX_WORDS_PER_CHUNK = 45
DEFAULT_SCALE = 1.4          # pixels per PDF point (~100 dpi)
SENTENCE_END = re.compile(r"[.!?][\"'\u2019\u201d)\]]*$")


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


def make_chunks(words, start=0, max_words=MAX_WORDS_PER_CHUNK,
                sentences=SENTENCES_PER_CHUNK):
    """Turn a word stream into (start_index, end_index, text) utterances."""
    chunks, cur, chunk_start, count = [], [], start, 0
    for i in range(start, len(words)):
        cur.append(i)
        at_end = bool(SENTENCE_END.search(words[i].text))
        if at_end:
            count += 1
        if (at_end and count >= sentences) or len(cur) >= max_words:
            chunks.append((chunk_start, i + 1,
                           " ".join(words[j].text for j in cur)))
            cur, chunk_start, count = [], i + 1, 0
    if cur:
        chunks.append((chunk_start, len(words),
                       " ".join(words[j].text for j in cur)))

    # Lone punctuation (list markers, running heads) makes a useless
    # utterance. Fold such fragments into their neighbour.
    merged = []
    for start, end, text in chunks:
        prev_start = merged[-1][0] if merged else None
        if prev_start is not None and end - start < 3 \
                and end - prev_start <= max_words:
            merged[-1] = (prev_start, end,
                          " ".join(w.text for w in words[prev_start:end]))
        else:
            merged.append((start, end, text))
    return merged


# --------------------------------------------------------------------------
# Speech controller.  Runs no threads of its own: speechd delivers END /
# CANCEL on its own thread, and we marshal everything back to GTK's main loop.
# --------------------------------------------------------------------------

class Speaker:
    def __init__(self, words, on_chunk, on_state):
        self.words = words
        self.on_chunk = on_chunk
        self.on_state = on_state
        self.client = speechd.SSIPClient("pdf-speaker")
        self.client.set_priority(speechd.Priority.TEXT)
        self.chunks = []
        self.idx = 0
        self.playing = False
        self.epoch = 0                 # stale-callback guard
        self.lock = threading.Lock()

    # -- internal ---------------------------------------------------------
    def _event(self, event_type, epoch):
        GLib.idle_add(self._event_main, event_type, epoch)

    def _event_main(self, event_type, epoch):
        with self.lock:
            if epoch != self.epoch or not self.playing:
                return False
            if event_type == CallbackType.END:
                self.idx += 1
                if self.idx >= len(self.chunks):
                    self.playing = False
                    done = True
                else:
                    done = False
            else:
                return False
        if done:
            self.on_state(False)
            self.on_chunk(None)
        else:
            self._speak_current()
        return False

    def _speak_current(self):
        with self.lock:
            if not self.playing or self.idx >= len(self.chunks):
                return
            start, end, text = self.chunks[self.idx]
            epoch = self.epoch
        self.on_chunk((start, end))
        # Must not call back into SSIP from the callback thread -> idle_add.
        self.client.speak(
            text,
            callback=lambda event_type, e=epoch: self._event(event_type, e),
            event_types=(CallbackType.END, CallbackType.CANCEL),
        )

    # -- public -----------------------------------------------------------
    def cue(self, word_index):
        """Point the reader at a word without starting playback."""
        with self.lock:
            self.epoch += 1
            self.playing = False
            self.chunks = make_chunks(self.words, word_index)
            self.idx = 0
        self.client.cancel()
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
        self.client.stop()
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
        self.client.cancel()
        self.on_state(False)
        self.on_chunk(None)

    def close(self):
        try:
            self.client.cancel()
            self.client.close()
        except Exception:
            pass


# --------------------------------------------------------------------------
# Xpdf-ish UI: toolbar, one page at a time, click anywhere to set the cursor.
# --------------------------------------------------------------------------

class App:
    def __init__(self, path=None):
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
            self.speaker = Speaker(words, self.on_chunk, self.on_state)
        except Exception as exc:
            self.speaker = None
            self.error("Speech Dispatcher unavailable", str(exc))
        else:
            try:
                self.apply_rate()
            except Exception:
                pass
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
        """Convert the 0.5–2.0 multiplier to SSIP's -100..100 rate.

        In this piper.conf, GenericRateMultiply 1 / GenericRateAdd 1 give
        $RATE = (ssip_rate * 0.01) + 1, so the two scales line up exactly.
        """
        if not self.speaker:
            return
        multiplier = self.rate.get_value()
        self.speaker.client.set_rate(int(round((multiplier - 1.0) * 100)))

    def on_rate(self, scale):
        self.apply_rate()
        if not rate_is_wired() and not self.rate_warned:
            self.rate_warned = True
            self.status.push(1, "Speed needs sox, or --length-scale $RATE "
                                "in piper.conf — see README")

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
        Gtk.main()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", nargs="?", help="PDF file to open")
    args = parser.parse_args()
    App(args.pdf).run()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
