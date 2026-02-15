#!/usr/bin/env python3
"""evilSDR - Lean, non-blocking SDR server with recording support."""

import asyncio
import json
import logging
import mimetypes
import time
import wave
import struct as pystruct
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import numpy as np

try:
    import websockets
    from websockets.asyncio.server import serve
except ImportError:
    print("[!] pip install websockets")
    raise

from dsp import RadioDSP
from rtl_client import RTLTCPClient
from scanner import Scanner
from decoders.pocsag import POCSAGDecoder

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
logger = logging.getLogger("evilSDR")

WS_HOST = "0.0.0.0"
WS_PORT = 8765
HTTP_PORT = 5555
RTL_HOST = "127.0.0.1"
RTL_PORT = 1234
SAMPLE_RATE = 2_400_000
FFT_SIZE = 2048
DEFAULT_FREQ = 88_700_000
READ_SIZE = 131072

FRONTEND_DIR = Path(__file__).parent.parent / "frontend"
BOOKMARKS_FILE = Path(__file__).parent / "bookmarks.json"
RECORDINGS_DIR = Path(__file__).parent.parent.parent / "recordings"
RECORDINGS_DIR.mkdir(exist_ok=True)


class SDRServer:
    def __init__(self):
        self.rtl = RTLTCPClient(host=RTL_HOST, port=RTL_PORT)
        self.dsp = RadioDSP(sample_rate=SAMPLE_RATE, fft_size=FFT_SIZE)
        self.clients = {}
        self.streaming = False
        self.running = True
        self._executor = ThreadPoolExecutor(max_workers=1)
        self._loop = None
        self._raw_queue = asyncio.Queue(maxsize=20)
        self.bookmarks = self._load_bookmarks()

        # Scanner & Decoder
        self.scanner = Scanner(self.rtl, self.dsp, bookmarks_file=BOOKMARKS_FILE)
        self.scanner._on_freq_change = self._on_scanner_freq_change
        self.scanner._on_mode_change = self._on_scanner_mode_change
        self.scanner._on_status_change = self._broadcast_scan_status
        self.pocsag = POCSAGDecoder()
        self.pocsag.set_callback(self._broadcast_pocsag)
        self.decode_pocsag = False

        # IQ Recording (toggle)
        self.iq_recording = False
        self.iq_capture_file = None
        self.iq_capture_filename = None

        # Audio Recording (toggle)
        self.audio_recording = False
        self.audio_wav_file = None
        self.audio_wav_filename = None

    async def _on_scanner_freq_change(self, freq):
        self._broadcast(json.dumps({"type": "FREQ_CHANGED", "value": freq}))

    async def _on_scanner_mode_change(self, mode):
        self._broadcast(json.dumps({"type": "MODE_CHANGED", "mode": mode}))

    async def _broadcast_scan_status(self, status):
        self._broadcast(json.dumps(status))

    def _broadcast_pocsag(self, message):
        self._broadcast(json.dumps({"type": "POCSAG", "message": message.to_dict()}))

    def _load_bookmarks(self):
        try:
            return json.loads(BOOKMARKS_FILE.read_text()) if BOOKMARKS_FILE.exists() else {"categories": []}
        except Exception:
            return {"categories": []}

    def _save_bookmarks(self, data):
        try:
            BOOKMARKS_FILE.write_text(json.dumps(data, indent=2))
            self.bookmarks = data
            return True
        except Exception:
            return False

    def _start_iq_recording(self):
        if self.iq_recording:
            return
        fname = f"iq_{int(time.time())}.raw"
        fpath = RECORDINGS_DIR / fname
        self.iq_capture_file = open(fpath, "wb")
        self.iq_capture_filename = fname
        self.iq_recording = True
        logger.info(f"IQ recording started: {fname}")
        self._broadcast(json.dumps({"type": "RECORD_STATUS", "iq": True, "iq_file": fname,
                                     "audio": self.audio_recording}))

    def _stop_iq_recording(self):
        if not self.iq_recording:
            return
        try:
            self.iq_capture_file.close()
        except Exception:
            pass
        self.iq_capture_file = None
        logger.info(f"IQ recording stopped: {self.iq_capture_filename}")
        self.iq_recording = False
        self._broadcast(json.dumps({"type": "RECORD_STATUS", "iq": False, "audio": self.audio_recording}))
        self.iq_capture_filename = None

    def _start_audio_recording(self):
        if self.audio_recording:
            return
        fname = f"audio_{int(time.time())}.wav"
        fpath = RECORDINGS_DIR / fname
        self.audio_wav_file = wave.open(str(fpath), "wb")
        self.audio_wav_file.setnchannels(1)
        self.audio_wav_file.setsampwidth(2)  # 16-bit
        self.audio_wav_file.setframerate(48000)
        self.audio_wav_filename = fname
        self.audio_recording = True
        logger.info(f"Audio recording started: {fname}")
        self._broadcast(json.dumps({"type": "RECORD_STATUS", "audio": True, "audio_file": fname,
                                     "iq": self.iq_recording}))

    def _stop_audio_recording(self):
        if not self.audio_recording:
            return
        try:
            self.audio_wav_file.close()
        except Exception:
            pass
        self.audio_wav_file = None
        logger.info(f"Audio recording stopped: {self.audio_wav_filename}")
        self.audio_recording = False
        self._broadcast(json.dumps({"type": "RECORD_STATUS", "audio": False, "iq": self.iq_recording}))
        self.audio_wav_filename = None

    async def register(self, ws):
        queue = asyncio.Queue(maxsize=100)
        self.clients[ws] = {"queue": queue, "audio": None}
        asyncio.create_task(self._client_sender(ws, queue))

        logger.info(f"Client connected ({len(self.clients)} total)")
        await ws.send(json.dumps({
            "type": "STATE",
            "mode": self.dsp.mode,
            "squelch": self.dsp.squelch_threshold,
            "streaming": self.streaming,
            "freq": self.rtl.center_freq,
            "sample_rate": self.rtl.sample_rate,
            "fft_size": FFT_SIZE,
            "iq_recording": self.iq_recording,
            "audio_recording": self.audio_recording,
        }))

    async def unregister(self, ws):
        self.clients.pop(ws, None)
        logger.info(f"Client disconnected ({len(self.clients)} total)")

    async def _client_sender(self, ws, queue):
        try:
            while True:
                msg = await queue.get()
                try:
                    await ws.send(msg)
                    client = self.clients.get(ws)
                    if client and client["audio"] is not None:
                        audio_msg = client["audio"]
                        client["audio"] = None
                        await ws.send(audio_msg)
                except Exception:
                    break
        finally:
            await self.unregister(ws)

    def _broadcast(self, msg, audio=False):
        if not self._loop:
            return
        for client_info in self.clients.values():
            if audio:
                client_info["audio"] = msg
            else:
                try:
                    client_info["queue"].put_nowait(msg)
                except asyncio.QueueFull:
                    pass

    async def handle_message(self, ws, message):
        try:
            msg = json.loads(message)
            t = msg.get("type", "")
            if t == "START_STREAM":
                self.streaming = True
                self._broadcast(json.dumps({"type": "STREAM_STATE", "streaming": True}))
            elif t == "STOP_STREAM":
                self.streaming = False
                self._broadcast(json.dumps({"type": "STREAM_STATE", "streaming": False}))
            elif t == "SET_MODE":
                self.dsp.set_mode(msg.get("mode", "FM"))
                self._broadcast(json.dumps({"type": "MODE_CHANGED", "mode": self.dsp.mode}))
            elif t == "SET_SQUELCH":
                self.dsp.set_squelch(float(msg.get("value", -60)))
                self._broadcast(json.dumps({"type": "SQUELCH_CHANGED", "value": self.dsp.squelch_threshold}))
            elif t == "SET_FREQ":
                await self.rtl.set_center_freq(int(msg.get("value", 100000000)))
                self._broadcast(json.dumps({"type": "FREQ_CHANGED", "value": self.rtl.center_freq}))
            elif t == "SET_GAIN":
                await self.rtl.set_gain(int(msg.get("value", 400)))
            elif t == "SET_AGC":
                await self.rtl.set_agc(1 if msg.get("value") else 0)
            elif t == "START_SCAN":
                await self.scanner.start(category_name=msg.get("category"))
            elif t == "STOP_SCAN":
                await self.scanner.stop()
            elif t == "SKIP_SCAN":
                await self.scanner.skip()
            elif t == "SET_SCAN_SPEED":
                self.scanner.set_speed(int(msg.get("value", 100)))
            elif t == "SET_SCAN_DELAY":
                self.scanner.set_resume_delay(float(msg.get("value", 2.0)))
            elif t == "TOGGLE_POCSAG":
                self.decode_pocsag = bool(msg.get("value", False))
                logger.info(f"POCSAG decoder {'enabled' if self.decode_pocsag else 'disabled'}")
            elif t == "GET_SCAN_CATEGORIES":
                cats = self.scanner.get_categories()
                try:
                    await ws.send(json.dumps({"type": "SCAN_CATEGORIES", "categories": cats}))
                except Exception:
                    pass
            # Recording commands
            elif t == "START_IQ_RECORD":
                self._start_iq_recording()
            elif t == "STOP_IQ_RECORD":
                self._stop_iq_recording()
            elif t == "START_AUDIO_RECORD":
                self._start_audio_recording()
            elif t == "STOP_AUDIO_RECORD":
                self._stop_audio_recording()
        except Exception as e:
            logger.error(f"handle_message error: {e}")

    async def ws_handler(self, ws):
        await self.register(ws)
        try:
            async for message in ws:
                if isinstance(message, str):
                    await self.handle_message(ws, message)
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            await self.unregister(ws)

    async def reader_loop(self):
        while self.running:
            if not self.rtl.connected:
                await asyncio.sleep(0.5)
                continue
            try:
                data = await self.rtl.reader.readexactly(READ_SIZE)
                try:
                    self._raw_queue.put_nowait(data)
                except asyncio.QueueFull:
                    pass
            except Exception as e:
                logger.error(f"Read error: {e}")
                self.rtl.connected = False
                await asyncio.sleep(0.1)

    def _process_chunk(self, data, streaming, dsp, decoder, decode_enabled,
                       iq_recording, iq_file, audio_recording, audio_wav):
        """Run in thread pool."""
        # IQ recording
        if iq_recording and iq_file:
            try:
                iq_file.write(data)
            except Exception:
                pass

        raw = np.frombuffer(data, dtype=np.uint8).astype(np.float32)
        raw = (raw - 127.5) / 127.5
        iq = raw[0::2] + 1j * raw[1::2]
        fft = dsp.compute_fft(iq)

        # Demodulate if streaming, decoding, or audio recording
        should_demod = streaming or decode_enabled or audio_recording
        audio = dsp.demodulate(iq) if should_demod else None

        if decode_enabled and audio is not None:
            decoder.process_audio(audio)

        # Audio recording — write demodulated audio as 16-bit PCM to WAV
        if audio_recording and audio_wav and audio is not None and len(audio) > 0:
            try:
                pcm16 = np.clip(audio * 32767, -32768, 32767).astype(np.int16)
                audio_wav.writeframes(pcm16.tobytes())
            except Exception:
                pass

        return audio, fft

    async def processor_loop(self):
        sig_counter = 0
        while self.running:
            data = await self._raw_queue.get()

            try:
                audio, fft = await self._loop.run_in_executor(
                    self._executor, self._process_chunk,
                    data, self.streaming, self.dsp, self.pocsag, self.decode_pocsag,
                    self.iq_recording, self.iq_capture_file,
                    self.audio_recording, self.audio_wav_file
                )
                self._broadcast(b"\x01" + fft["magnitudes"].tobytes())
                if audio is not None and len(audio) > 0:
                    self._broadcast(b"\x02" + audio.tobytes(), audio=True)
                sig_counter += 1
                if sig_counter >= 10:
                    sig_counter = 0
                    self._broadcast(json.dumps({
                        "type": "SIGNAL_LEVEL",
                        "db": fft["signal_db"],
                        "min_db": fft["min_db"],
                        "max_db": fft["max_db"],
                        "s_units": self.dsp.dbfs_to_s_units(fft["signal_db"]),
                    }))
            except Exception as e:
                logger.error(f"Processing error: {e}")

    async def connection_loop(self):
        self._loop = asyncio.get_running_loop()
        asyncio.create_task(self.reader_loop())
        asyncio.create_task(self.processor_loop())
        delay = 2
        while self.running:
            try:
                logger.info(f"Connecting to rtl_tcp at {self.rtl.host}:{self.rtl.port}...")
                await self.rtl.connect()
                delay = 2
                await self.rtl.set_center_freq(DEFAULT_FREQ)
                self._broadcast(json.dumps({"type": "CONNECTION_CHANGED",
                    "host": self.rtl.host, "port": self.rtl.port, "connected": True, "freq": DEFAULT_FREQ}))
                while self.running and self.rtl.connected:
                    await asyncio.sleep(1)
            except Exception as e:
                logger.warning(f"RTL-TCP connection failed: {e}")
                self._broadcast(json.dumps({"type": "CONNECTION_CHANGED",
                    "host": self.rtl.host, "port": self.rtl.port, "connected": False}))
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)

    async def http_handler(self, reader, writer):
        try:
            req = await asyncio.wait_for(reader.read(65536), timeout=5)
            if not req:
                return
            parts = req.split(b"\r\n")[0].decode().split(" ")
            if len(parts) < 2:
                return
            method, path = parts[0], parts[1].split("?")[0]
            logger.info(f"HTTP Request: {method} {path}")

            if path == "/api/bookmarks":
                if method == "GET":
                    body = json.dumps(self.bookmarks).encode()
                    writer.write(f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\nAccess-Control-Allow-Origin: *\r\n\r\n".encode() + body)
                elif method == "POST":
                    try:
                        body_part = req.split(b"\r\n\r\n", 1)[1]
                        data = json.loads(body_part.decode())
                        if self._save_bookmarks(data):
                            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nAccess-Control-Allow-Origin: *\r\nContent-Length: 11\r\n\r\n{\"ok\":true}")
                        else:
                            writer.write(b"HTTP/1.1 500 Error\r\nAccess-Control-Allow-Origin: *\r\nContent-Length: 12\r\n\r\n{\"ok\":false}")
                    except Exception:
                        writer.write(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
                elif method == "OPTIONS":
                    writer.write(b"HTTP/1.1 204 No Content\r\nAccess-Control-Allow-Origin: *\r\nAccess-Control-Allow-Methods: GET, POST, OPTIONS\r\nAccess-Control-Allow-Headers: Content-Type\r\n\r\n")
            else:
                fpath = FRONTEND_DIR / (path.lstrip("/") or "index.html")
                if fpath.is_file():
                    body = fpath.read_bytes()
                    ct = mimetypes.guess_type(str(fpath))[0] or "application/octet-stream"
                    writer.write(f"HTTP/1.1 200 OK\r\nContent-Type: {ct}\r\nContent-Length: {len(body)}\r\nAccess-Control-Allow-Origin: *\r\n\r\n".encode() + body)
                else:
                    writer.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
        except Exception:
            pass
        finally:
            writer.close()

    async def run(self):
        logger.info(f"evilSDR starting — WS:{WS_PORT} HTTP:{HTTP_PORT}")
        asyncio.create_task(self.connection_loop())
        async with serve(self.ws_handler, WS_HOST, WS_PORT):
            http_server = await asyncio.start_server(self.http_handler, WS_HOST, HTTP_PORT)
            logger.info(f"HTTP serving {FRONTEND_DIR} on :{HTTP_PORT}")
            await http_server.serve_forever()


if __name__ == "__main__":
    asyncio.run(SDRServer().run())
