#!/usr/bin/env python3
"""evilSDR Phase 1 MVP - Lean, non-blocking SDR server."""

import asyncio
import json
import logging
import mimetypes
import time
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
DEFAULT_FREQ = 88_700_000 # NPR WBFO
READ_SIZE = 131072  # ~27ms at 2.4MSPS

FRONTEND_DIR = Path(__file__).parent.parent / "frontend"


BOOKMARKS_FILE = Path(__file__).parent / "bookmarks.json"


class SDRServer:
    def __init__(self):
        self.rtl = RTLTCPClient(host=RTL_HOST, port=RTL_PORT)
        self.dsp = RadioDSP(sample_rate=SAMPLE_RATE, fft_size=FFT_SIZE)
        self.clients = {}
        self.streaming = False
        self.running = True
        self._executor = ThreadPoolExecutor(max_workers=4)
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
        self.iq_capture_file = None
        self.iq_stop_time = 0

    async def _on_scanner_freq_change(self, freq):
        """Broadcast frequency change from scanner to all clients."""
        self._broadcast(json.dumps({"type": "FREQ_CHANGED", "value": freq}))

    async def _on_scanner_mode_change(self, mode):
        """Broadcast mode change from scanner to all clients."""
        self._broadcast(json.dumps({"type": "MODE_CHANGED", "mode": mode}))

    async def _broadcast_scan_status(self, status):
        """Broadcast scanner state."""
        self._broadcast(json.dumps(status))
        
    def _broadcast_pocsag(self, message):
        """Broadcast decoded POCSAG message."""
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

    async def register(self, ws):
        queue = asyncio.Queue(maxsize=100)
        self.clients[ws] = queue
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
                except Exception:
                    break
        finally:
            await self.unregister(ws)

    def _broadcast(self, msg):
        if not self._loop:
            return
        for q in self.clients.values():
            try:
                self._loop.call_soon_threadsafe(q.put_nowait, msg)
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
            elif t == "CAPTURE_IQ_SNAPSHOT":
                duration = int(msg.get("duration", 10))
                fname = f"capture_{int(time.time())}.iq"
                self.iq_capture_file = open(fname, "wb")
                self.iq_stop_time = time.time() + duration
                logger.info(f"Starting IQ capture ({duration}s) to {fname}")
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
        """Read raw bytes from RTL-TCP socket — never block."""
        while self.running:
            if not self.rtl.connected:
                await asyncio.sleep(0.5)
                continue
            try:
                data = await self.rtl.reader.readexactly(READ_SIZE)
                try:
                    self._raw_queue.put_nowait(data)
                except asyncio.QueueFull:
                    pass  # drop oldest implicitly by not blocking
            except Exception as e:
                logger.error(f"Read error: {e}")
                self.rtl.connected = False
                await asyncio.sleep(0.1)

    def _process_chunk(self, data, streaming, dsp, decoder, decode_enabled, capture_file):
        """Run in thread pool — converts bytes, computes FFT, optionally demods."""
        # Capture raw IQ if enabled
        if capture_file:
            try:
                capture_file.write(data)
            except Exception:
                pass

        raw = np.frombuffer(data, dtype=np.uint8).astype(np.float32)
        raw = (raw - 127.5) / 127.5
        iq = raw[0::2] + 1j * raw[1::2]
        fft = dsp.compute_fft(iq)
        
        # Demodulate if streaming OR decoding
        should_demod = streaming or decode_enabled
        audio = dsp.demodulate(iq) if should_demod else None
        
        if decode_enabled and audio is not None:
            decoder.process_audio(audio)
            
        return audio, fft

    async def processor_loop(self):
        """Consume raw queue, offload DSP to threads."""
        sig_counter = 0
        while self.running:
            data = await self._raw_queue.get()
            
            # Check capture timeout
            if self.iq_capture_file and time.time() > self.iq_stop_time:
                try:
                    self.iq_capture_file.close()
                except:
                    pass
                self.iq_capture_file = None
                logger.info("IQ capture complete")

            try:
                audio, fft = await self._loop.run_in_executor(
                    self._executor, self._process_chunk,
                    data, self.streaming, self.dsp, self.pocsag, self.decode_pocsag, self.iq_capture_file
                )
                # Broadcast FFT (prefix 0x01)
                self._broadcast(b"\x01" + fft["magnitudes"].tobytes())
                # Broadcast audio (prefix 0x02)
                if audio is not None and len(audio) > 0:
                    self._broadcast(b"\x02" + audio.tobytes())
                # Signal level every ~10 chunks
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
        """Manage RTL-TCP connection with auto-reconnect."""
        self._loop = asyncio.get_running_loop()
        asyncio.create_task(self.reader_loop())
        asyncio.create_task(self.processor_loop())
        delay = 2
        while self.running:
            try:
                logger.info(f"Connecting to rtl_tcp at {self.rtl.host}:{self.rtl.port}...")
                await self.rtl.connect()
                delay = 2
                
                # Default frequency on initial connect
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
            if not req: return
            parts = req.split(b"\r\n")[0].decode().split(" ")
            if len(parts) < 2:
                return
            method, path = parts[0], parts[1].split("?")[0]
            logger.info(f"HTTP Request: {method} {path}")

            if path == "/api/bookmarks":
                if method == "GET":
                    body = json.dumps(self.bookmarks).encode()
                    writer.write(f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\nAccess-Control-Allow-Origin: *\r\n\r\n".encode() + body)
                    logger.info(f"Served {len(self.bookmarks['categories'])} categories")
                elif method == "POST":
                    try:
                        # Simple body extraction
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
