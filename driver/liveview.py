"""Decode the camera's live HEVC stream into JPEG frames for the panel.

The camera sends video as `pktType 0x02` datagrams on the same UDP socket that
carries control. `hevc.HevcDepacketizer` reassembles those into Annex-B access
units; this feeds them to ffmpeg (via PyAV) and publishes JPEGs that the
browser consumes as an MJPEG stream.

Two deliberate choices:

* **Control outranks preview.** Decoding happens on its own thread behind a
  bounded queue, and the queue drops the *oldest* frame when full. A late
  preview frame is worthless, and blocking the datalink receive loop to decode
  video would delay telemetry and stick packets -- which is the traffic that
  actually moves the camera.

* **Wait for a keyframe.** A decoder handed a mid-GOP access unit produces
  nothing and looks broken. Units are discarded until one carries parameter
  sets or an IDR slice.
"""

from __future__ import annotations

import logging
import queue
import threading
import time

from . import hevc

log = logging.getLogger(__name__)

# Small on purpose: preview latency matters more than preview completeness.
QUEUE_DEPTH = 4
# If this many packets in a row yield no frame, the decoder is wedged on a
# missing reference. Rebuild it and wait for the next real IDR rather than
# staying dark forever.
# Two tiers, because the two failures need opposite treatment.
#
# A decoder that is merely starved -- a network gap, a burst that arrived late
# -- is healthy and must be left alone. Asking the camera for a fresh entry
# point costs nothing and fixes the case where we genuinely lost the reference.
#
# Rebuilding the decoder is the destructive option and comes last: this stream
# carries about two IDRs in twelve thousand frames, so a decoder that has
# thrown away its reference pictures stays blank until the camera grants one.
# That is what turned a one-second hiccup into a permanent black frame.
STARVATION_LIMIT = 40        # ~1.3 s: ask for a keyframe
HARD_RESET_LIMIT = 300       # ~10 s: nothing worked, rebuild the decoder
KEYFRAME_REQUEST_COOLDOWN_S = 2.0

# Annex-B start code, written this way so it survives shell heredocs.
START_CODE = bytes([0, 0, 1])
JPEG_QUALITY = 80
# Preview width. The panel shows this in a box a few hundred pixels wide and
# the scopes sample it down to 320 anyway, so full 1280x720 JPEGs cost real
# time for detail nobody sees. Scaling in ffmpeg is far cheaper than encoding
# the extra pixels.
PREVIEW_WIDTH = 960


class LiveView:
    """Owns the decode thread and the most recent JPEG."""

    def __init__(self, jpeg_quality: int = JPEG_QUALITY):
        self.jpeg_quality = jpeg_quality
        self.depacketizer = hevc.HevcDepacketizer()

        self.jpeg: bytes | None = None
        self.width = 0
        self.height = 0
        self.frames_decoded = 0
        self.frames_dropped = 0      # dropped by us, queue full
        self.decode_errors = 0
        # Separates "no units arrive" from "parser swallows them" from
        # "decoder returns nothing" -- three failures that look identical.
        self.units_pulled = 0
        self.packets_parsed = 0
        self.units_no_packet = 0
        self.packets_no_frame = 0
        self.resyncs = 0
        # What are we actually handing the decoder? A NAL-type histogram over
        # the first units settles in one run what guessing could not.
        self.nal_hist: dict[int, int] = {}
        self.unit_shapes: list[str] = []
        # Parameter sets arrive in their OWN access unit and only rarely -- on
        # this camera, once for the whole session. The IDR that follows does
        # not carry them. Joining on the IDR alone gives the decoder a picture
        # it cannot configure for, so it silently returns nothing forever.
        # Cache them and prepend to the unit we start on.
        self._param_sets: dict[int, bytes] = {}
        self._replay_params = False
        self._reframer = hevc.AudReframer()
        # Called when the decoder needs a fresh entry point. This camera emits
        # about two IDRs in twelve thousand frames, so waiting for one is not
        # a recovery strategy -- we have to ask for it.
        self.on_need_keyframe = None
        self._last_keyframe_request = 0.0
        self.keyframe_requests = 0
        self.discarded_waiting = 0
        self.last_frame_at = 0.0
        self.started_keyframe = False
        self.error: str | None = None

        self._queue: queue.Queue[bytes] = queue.Queue(maxsize=QUEUE_DEPTH)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # A single shared Event cannot serve several viewers: whichever one
        # calls clear() first makes the others miss that frame, so two clients
        # each receive a fraction of the stream and it looks unstable. A
        # monotonic counter with a Condition lets every client wait for "a
        # frame newer than the one I last sent" independently.
        self._frame_no = 0
        self._frames = threading.Condition()
        self._lock = threading.Lock()

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._decode_loop,
                                        name="liveview-decode", daemon=True)
        self._thread.start()
        # Long GIL holds elsewhere (serial formatting, state dicts) were enough
        # to stall decoding entirely. Give the interpreter more instructions
        # between switches so a decode of a 720p frame is not chopped up.
        import sys
        if sys.getswitchinterval() < 0.01:
            sys.setswitchinterval(0.01)
        log.info("live view decoder started")

    def stop(self) -> None:
        self._stop.set()
        with self._frames:
            self._frames.notify_all()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def fps(self) -> float:
        return round(self._fps, 1)

    # -- ingest -------------------------------------------------------------

    def feed_datagram(self, payload: bytes) -> None:
        """Called from the datalink receive loop. Must never block."""
        raw = self.depacketizer.feed(payload)
        if raw is None:
            return
        # Re-cut on access unit delimiters: the camera's fragment counter runs
        # one NAL late, so what the depacketiser hands us isshifted by one.
        for unit in self._reframer.feed(raw):
            self._offer(unit)

    def _offer(self, unit: bytes) -> None:
        nals = [n for n in hevc.nal_units(unit) if n]
        types = [hevc.nal_type(n[0]) for n in nals]

        # Parameter sets arrive in their own access unit and only rarely -- on
        # this camera, once for the whole session, never with the IDR. Cache
        # them or the decoder gets a picture it cannot configure for and
        # silently returns nothing.
        for n, t in zip(nals, types):
            if t in (hevc.NAL_VPS, hevc.NAL_SPS, hevc.NAL_PPS):
                self._param_sets[t] = hevc.START_CODE + n

        if len(self.unit_shapes) < 12:
            self.unit_shapes.append(f"{len(unit)}B {types}")
        for t in types:
            self.nal_hist[t] = self.nal_hist.get(t, 0) + 1

        if not self.started_keyframe:
            if not all(t in self._param_sets
                       for t in (hevc.NAL_VPS, hevc.NAL_SPS, hevc.NAL_PPS)):
                return
            if not any(hevc.is_random_access_slice(t) for t in types):
                # Joining mid-stream there may be no IDR ahead of us for
                # minutes. Ask rather than wait; the cooldown keeps it polite.
                self.discarded_waiting += 1
                if self.discarded_waiting % STARVATION_LIMIT == 0:
                    self._ask_for_keyframe()
                return
            unit = (self._param_sets[hevc.NAL_VPS]
                    + self._param_sets[hevc.NAL_SPS]
                    + self._param_sets[hevc.NAL_PPS]
                    + unit)
            self.started_keyframe = True
            log.info("live view: parameter sets + keyframe, decoding")

        # After a rebuild the fresh decoder needs the configuration again.
        if self._replay_params and self.started_keyframe:
            self._replay_params = False
            unit = (self._param_sets[hevc.NAL_VPS]
                    + self._param_sets[hevc.NAL_SPS]
                    + self._param_sets[hevc.NAL_PPS]
                    + unit)

        try:
            self._queue.put_nowait(unit)
        except queue.Full:
            try:
                self._queue.get_nowait()      # drop the oldest, keep the newest
                self._queue.put_nowait(unit)
                self.frames_dropped += 1
            except (queue.Empty, queue.Full):
                pass

    # -- output -------------------------------------------------------------

    def wait_for_frame(self, since: int = -1, timeout: float = 2.0
                       ) -> tuple[bytes | None, int]:
        """Block until a frame newer than `since`. Returns (jpeg, frame_no).

        Per-client rather than shared: several viewers can each stream the
        full rate without stealing each other's wakeups.
        """
        with self._frames:
            if self._frame_no <= since:
                self._frames.wait(timeout)
            if self._frame_no <= since:
                return (None, since)
            n = self._frame_no
        with self._lock:
            return (self.jpeg, n)

    def snapshot(self) -> bytes | None:
        with self._lock:
            return self.jpeg

    # -- decode -------------------------------------------------------------

    _fps = 0.0

    def _decode_loop(self) -> None:
        import av                      # imported here so the driver works without it
        import fractions

        codec = av.CodecContext.create("hevc", "r")
        # Encode with ffmpeg rather than Pillow. The old path pulled every
        # frame into a numpy RGB array and compressed it in Python, which at
        # 720p30 is ~80 MB/s of memory traffic plus a pure-Python encode --
        # it fell behind, the queue filled and frames dropped in bursts, which
        # looks exactly like an unstable link.
        enc = None
        recent: list[float] = []
        barren = 0          # consecutive packets that produced no frame

        def make_encoder(w: int, h: int):
            e = av.CodecContext.create("mjpeg", "w")
            e.width, e.height = w, h
            e.pix_fmt = "yuvj420p"
            e.time_base = fractions.Fraction(1, 30)
            # 2..31, lower is better. 4 is visually clean at preview size.
            e.qmin = e.qmax = 4
            return e

        while not self._stop.is_set():
            try:
                unit = self._queue.get(timeout=0.3)
            except queue.Empty:
                continue
            self.units_pulled += 1
            try:
                got_frame = False
                for packet in codec.parse(unit):
                    self.packets_parsed += 1
                    for frame in codec.decode(packet):
                        got_frame = True
                        w = min(PREVIEW_WIDTH, frame.width)
                        h = max(2, int(frame.height * w / frame.width) & ~1)
                        if enc is None or enc.width != w or enc.height != h:
                            enc = make_encoder(w, h)
                        small = frame.reformat(width=w, height=h,
                                               format="yuvj420p")
                        small.pts = None
                        blob = b"".join(bytes(pkt) for pkt in enc.encode(small))
                        if not blob:
                            continue
                        with self._lock:
                            self.jpeg = blob
                            self.width, self.height = frame.width, frame.height
                        self.frames_decoded += 1
                        self.last_frame_at = time.time()
                        recent.append(self.last_frame_at)
                        if len(recent) > 15:
                            recent.pop(0)
                        if len(recent) > 1:
                            span = recent[-1] - recent[0]
                            self._fps = (len(recent) - 1) / span if span > 0 else 0.0
                        with self._frames:
                            self._frame_no += 1
                            self._frames.notify_all()

                if got_frame:
                    barren = 0
                else:
                    self.packets_no_frame += 1
                    barren += 1
                    if barren == STARVATION_LIMIT or (
                            barren > STARVATION_LIMIT
                            and barren % STARVATION_LIMIT == 0):
                        self._ask_for_keyframe()
                    if barren >= HARD_RESET_LIMIT:
                        barren = 0
                        self.resyncs += 1
                        log.warning("no frame for %d units -- rebuilding the "
                                    "decoder", HARD_RESET_LIMIT)
                        codec = av.CodecContext.create("hevc", "r")
                        # Re-attach the cached configuration to the next unit.
                        # started_keyframe deliberately stays True: going back
                        # to waiting for an IDR is what stranded this pipeline.
                        self._replay_params = True
                        self._ask_for_keyframe(force=True)
            except Exception as exc:          # a bad unit must not kill the thread
                self.decode_errors += 1
                if self.decode_errors <= 5:
                    log.info("decode error: %s", exc)

    def _ask_for_keyframe(self, force: bool = False) -> None:
        """Ask the camera to restart its GOP. Rate limited -- the request is
        cheap but it does interrupt the encoder, so spamming it would make the
        picture worse than the stall it is fixing."""
        if not self.on_need_keyframe:
            return
        now = time.time()
        if not force and now - self._last_keyframe_request < KEYFRAME_REQUEST_COOLDOWN_S:
            return
        self._last_keyframe_request = now
        self.keyframe_requests += 1
        log.info("decoder starved -- requesting a keyframe from the camera")
        try:
            self.on_need_keyframe()
        except Exception:
            log.exception("on_need_keyframe raised")

    # -- diagnostics --------------------------------------------------------

    def stats(self) -> dict:
        return {
            "running": self.running,
            "decoded": self.frames_decoded,
            "fps": self.fps,
            "width": self.width,
            "height": self.height,
            "dropped_late": self.frames_dropped,
            "dropped_incomplete": self.depacketizer.dropped_incomplete,
            "fed": self.depacketizer.fed,
            "accepted": self.depacketizer.accepted,
            "frame_changes": self.depacketizer.frame_changes,
            "distinct_frame_ids": len(self.depacketizer.frame_ids),
            "max_buffer": self.depacketizer.max_buffer,
            "queued": self._queue.qsize(),
            "units_pulled": self.units_pulled,
            "packets_parsed": self.packets_parsed,
            "units_no_packet": self.units_no_packet,
            "packets_no_frame": self.packets_no_frame,
            "keyframe_seen": self.started_keyframe,
            "preview_width": PREVIEW_WIDTH,
            "decode_errors": self.decode_errors,
            "resyncs": self.resyncs,
            "keyframe_requests": self.keyframe_requests,
            "discarded_waiting": self.discarded_waiting,
            "reframed": self._reframer.emitted,
            "reframe_overflows": self._reframer.overflows,
            "carry_bytes": len(self._reframer.carry),
            "nal_hist": dict(sorted(self.nal_hist.items())),
            "unit_shapes": self.unit_shapes[:12],
            "have_frame": self.jpeg is not None,
            "stale": (time.time() - self.last_frame_at) > 2.0 if self.last_frame_at else True,
        }
