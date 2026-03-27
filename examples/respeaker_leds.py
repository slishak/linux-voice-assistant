#!/usr/bin/env python3
"""ReSpeaker 2-mic Pi HAT LED controller for linux-voice-assistant.

Connects to the peripheral WebSocket API and drives the 3 APA102 LEDs
on the ReSpeaker 2-mic Pi HAT based on assistant state events.

Requirements (on the Pi):
    pip install websockets
    # SPI must be enabled: sudo raspi-config -> Interface Options -> SPI

Run:
    python3 respeaker_leds.py
    python3 respeaker_leds.py --uri ws://192.168.1.x:6055
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import struct
import time
from typing import Optional

_LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# APA102 SPI driver (3 LEDs on ReSpeaker 2-mic HAT)
# ---------------------------------------------------------------------------

NUM_LEDS = 3
SPI_DEV = "/dev/spidev0.0"
SPI_SPEED_HZ = 8_000_000


class APA102:
    """Minimal APA102 LED strip driver over SPI."""

    def __init__(self, num_leds: int = NUM_LEDS, spi_dev: str = SPI_DEV) -> None:
        self.num_leds = num_leds
        self._buf = [(0, 0, 0)] * num_leds  # (r, g, b)
        self._spi = None
        try:
            import spidev  # type: ignore[import]

            self._spi = spidev.SpiDev()
            bus, device = (int(x) for x in spi_dev.replace("/dev/spidev", "").split("."))
            self._spi.open(bus, device)
            self._spi.max_speed_hz = SPI_SPEED_HZ
            self._spi.mode = 0b01  # APA102 uses SPI mode 1
        except Exception as exc:
            _LOGGER.error("Failed to open SPI device %s: %s", spi_dev, exc)
            _LOGGER.error("LEDs will be simulated (no hardware output)")

    def set_pixel(self, index: int, r: int, g: int, b: int) -> None:
        self._buf[index] = (r, g, b)

    def set_all(self, r: int, g: int, b: int) -> None:
        for i in range(self.num_leds):
            self._buf[i] = (r, g, b)

    def off(self) -> None:
        self.set_all(0, 0, 0)
        self.show()

    def show(self, brightness: float = 1.0) -> None:
        bright5 = max(0, min(31, int(brightness * 31)))
        frame = [0x00, 0x00, 0x00, 0x00]  # start frame
        for r, g, b in self._buf:
            frame += [
                0xE0 | bright5,
                int(b * brightness),
                int(g * brightness),
                int(r * brightness),
            ]
        # end frame: ceil(n/2) bytes of 0xFF
        frame += [0xFF] * math.ceil(self.num_leds / 2)

        if self._spi is not None:
            try:
                self._spi.xfer2(frame)
            except Exception as exc:
                _LOGGER.debug("SPI write error: %s", exc)
        else:
            # Simulate output
            pixels = ", ".join(f"rgb({r},{g},{b})" for r, g, b in self._buf)
            _LOGGER.debug("LEDs: [%s] brightness=%.2f", pixels, brightness)

    def close(self) -> None:
        self.off()
        if self._spi is not None:
            self._spi.close()


# ---------------------------------------------------------------------------
# LED animation patterns
# ---------------------------------------------------------------------------

# Colours  (r, g, b)
BLUE = (0, 80, 255)
CYAN = (0, 200, 200)
GREEN = (0, 200, 50)
YELLOW = (200, 180, 0)
RED = (255, 0, 0)
WHITE = (180, 180, 180)
ORANGE = (255, 80, 0)
DIM_RED = (60, 0, 0)


class LEDAnimator:
    """Drives APA102 animations based on assistant state."""

    def __init__(self, leds: APA102) -> None:
        self.leds = leds
        self._task: Optional[asyncio.Task] = None
        self._state = "idle"

    def set_state(self, state: str, data: dict | None = None) -> None:
        if self._state == state:
            return
        self._state = state
        self._cancel()

        if state == "idle":
            self._task = asyncio.create_task(self._idle())
        elif state == "wake_word_detected":
            self._task = asyncio.create_task(self._flash(BLUE, flashes=2))
        elif state == "listening":
            self._task = asyncio.create_task(self._spin(CYAN))
        elif state == "thinking":
            self._task = asyncio.create_task(self._pulse(YELLOW))
        elif state == "tts_speaking":
            self._task = asyncio.create_task(self._breathe(GREEN))
        elif state == "error":
            self._task = asyncio.create_task(self._flash(RED, flashes=3))
        elif state == "muted":
            self._task = asyncio.create_task(self._steady(DIM_RED))
        elif state == "timer_ringing":
            self._task = asyncio.create_task(self._flash(ORANGE, flashes=999, interval=0.4))
        elif state == "media_playing":
            self._task = asyncio.create_task(self._steady(GREEN, brightness=0.2))
        else:
            self._task = asyncio.create_task(self._idle())

    def _cancel(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()

    async def _idle(self) -> None:
        self.leds.off()

    async def _steady(self, colour: tuple, brightness: float = 0.5) -> None:
        self.leds.set_all(*colour)
        self.leds.show(brightness)

    async def _flash(self, colour: tuple, flashes: int = 2, interval: float = 0.15) -> None:
        for _ in range(flashes):
            self.leds.set_all(*colour)
            self.leds.show(1.0)
            await asyncio.sleep(interval)
            self.leds.off()
            await asyncio.sleep(interval)
        # After flashing stay off (caller will transition state)

    async def _spin(self, colour: tuple, period: float = 0.6) -> None:
        """One lit LED chases around the ring."""
        step_time = period / self.leds.num_leds
        pos = 0
        while True:
            self.leds.off()
            self.leds.set_pixel(pos % self.leds.num_leds, *colour)
            self.leds.show(1.0)
            pos += 1
            await asyncio.sleep(step_time)

    async def _pulse(self, colour: tuple, period: float = 1.0) -> None:
        """All LEDs brightness pulses smoothly."""
        while True:
            t = time.monotonic()
            brightness = 0.3 + 0.7 * (0.5 + 0.5 * math.sin(2 * math.pi * t / period))
            self.leds.set_all(*colour)
            self.leds.show(brightness)
            await asyncio.sleep(0.03)

    async def _breathe(self, colour: tuple, period: float = 2.0) -> None:
        """Slow breathe — good for speaking."""
        while True:
            t = time.monotonic()
            brightness = 0.2 + 0.8 * (0.5 + 0.5 * math.sin(2 * math.pi * t / period))
            self.leds.set_all(*colour)
            self.leds.show(brightness)
            await asyncio.sleep(0.03)

    def cleanup(self) -> None:
        self._cancel()
        self.leds.off()


# ---------------------------------------------------------------------------
# WebSocket client
# ---------------------------------------------------------------------------

async def run(uri: str) -> None:
    try:
        import websockets  # type: ignore[import]
    except ImportError:
        _LOGGER.error("websockets not installed. Run: pip install websockets")
        return

    leds = APA102()
    animator = LEDAnimator(leds)

    _LOGGER.info("Connecting to %s", uri)

    try:
        while True:
            try:
                async with websockets.connect(uri, ping_interval=20, ping_timeout=10) as ws:
                    _LOGGER.info("Connected")
                    async for raw in ws:
                        try:
                            msg = json.loads(raw)
                        except json.JSONDecodeError:
                            continue

                        event = msg.get("event", "")
                        data = msg.get("data", {})
                        _LOGGER.debug("Event: %s %s", event, data)

                        if event == "snapshot":
                            if data.get("muted"):
                                animator.set_state("muted")
                            else:
                                animator.set_state("idle")

                        elif event == "wake_word_detected":
                            animator.set_state("wake_word_detected")

                        elif event == "listening":
                            animator.set_state("listening")

                        elif event == "thinking":
                            animator.set_state("thinking")

                        elif event == "tts_speaking":
                            animator.set_state("tts_speaking")

                        elif event in ("tts_finished", "idle"):
                            if animator._state not in ("muted",):
                                animator.set_state("idle")

                        elif event == "muted":
                            animator.set_state("muted")

                        elif event == "error":
                            animator.set_state("error")
                            # Return to idle after showing error
                            await asyncio.sleep(2.0)
                            animator.set_state("idle")

                        elif event == "timer_ringing":
                            animator.set_state("timer_ringing")

                        elif event == "media_player_playing":
                            animator.set_state("media_playing")

                        elif event == "volume_muted":
                            if data.get("muted"):
                                animator.set_state("muted")
                            else:
                                animator.set_state("idle")

            except (OSError, websockets.exceptions.WebSocketException) as exc:
                _LOGGER.warning("Connection lost: %s — retrying in 5s", exc)
                animator.set_state("idle")
                await asyncio.sleep(5)
    finally:
        animator.cleanup()


def main() -> None:
    parser = argparse.ArgumentParser(description="ReSpeaker 2-mic HAT LED controller")
    parser.add_argument(
        "--uri",
        default="ws://localhost:6055",
        help="WebSocket URI of the peripheral API (default: ws://localhost:6055)",
    )
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO)

    try:
        asyncio.run(run(args.uri))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
