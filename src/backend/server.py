#!/usr/bin/env python3
"""evilSDR - Lean, non-blocking SDR server with recording support."""

import asyncio
import json
import os
import logging
import mimetypes
import time
import uuid
import wave
import struct as pystruct
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from threading import Lock

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
from decoders import load_decoders

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
logger = logging.getLogger("evilSDR")

CONFIG_FILE = Path(os.environ.get("EVILSDR_CONFIG_FILE", Path(__file__).parent / "config.json"))
BOOKMARKS_FILE = Path(os.environ.get("EVILSDR_BOOKMARKS_FILE", Path(__file__).parent / "bookmarks.json"))
CONNECTIONS_FILE = Path(os.environ.get("EVILSDR_CONNECTIONS_FILE", Path(__file__).parent / "connections.json"))
RECORDINGS_DIR = Path(os.environ.get("EVILSDR_RECORDINGS_DIR", Path(__file__).parent.parent.parent / "recordings"))

def load_config():
    defaults = {
        "ws_host": "0.0.0.0",
        "ws_port": 8765,
        "http_port": 5555,
        "rtl_host": "127.0.0.1",
        "rtl_port": 1234,
        "sample_rate": 2_400_000,
        "fft_size": 2048,
        "default_freq": 88_700_000
    }
    if CONFIG_FILE.exists():
        try:
            user_config = json.loads(CONFIG_FILE.read_text())
            defaults.update(user_config)
        except Exception as e:
            logger.error(f"Failed to load config: {e}")
    return defaults

config = load_config()

WS_HOST = config["ws_host"]
WS_PORT = config["ws_port"]
HTTP_PORT = config["http_port"]
RTL_HOST = config["rtl_host"]
RTL_PORT = config["rtl_port"]
SAMPLE_RATE = config["sample_rate"]
FFT_SIZE = config["fft_size"]
DEFAULT_FREQ = config["default_freq"]
READ_SIZE = 131072

FRONTEND_DIR = Path(__file__).parent.parent / "frontend"
DEFAULT_CONNECTION_ID = "local-rtl"
RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)


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
        self._connections_lock = Lock()
        self.connections = self._load_connections()
        self._desired_connection = None
        self._desired_connection_id = None
        self._desired_connection_nonce = 0
        self.bookmarks = self._load_bookmarks()
        
        # Thread safety locks
        self._clients_lock = Lock()  # Protects clients dict
        self._recording_lock = Lock()  # Protects recording state and file handles
        self._dsp_lock = Lock()  # Protects DSP state during reads

        # Scanner & Decoders (plugin architecture)
        self.scanner = Scanner(self.rtl, self.dsp, bookmarks_file=BOOKMARKS_FILE)
        self.scanner._on_freq_change = self._on_scanner_freq_change
        self.scanner._on_mode_change = self._on_scanner_mode_change
        self.scanner._on_status_change = self._broadcast_scan_status

        self.decoders = load_decoders(sample_rate=48000)
        for dec in self.decoders.values():
            dec.add_callback(self._broadcast_decoder_message)
        logger.info(f"Loaded {len(self.decoders)} decoder(s): {list(self.decoders.keys())}")

        # Legacy convenience alias
        self.pocsag = self.decoders.get("pocsag")
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
        # Flush raw queue to reduce tuning latency/stale signal levels
        while not self._raw_queue.empty():
            try:
                self._raw_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        self._broadcast(json.dumps({"type": "FREQ_CHANGED", "value": freq}))

    async def _on_scanner_mode_change(self, mode):
        self._broadcast(json.dumps({"type": "MODE_CHANGED", "mode": mode}))

    async def _broadcast_scan_status(self, status):
        self._broadcast(json.dumps(status))

    def _broadcast_pocsag(self, message):
        """Legacy — kept for backward compat."""
        self._broadcast(json.dumps({"type": "POCSAG", "message": message}))

    def _broadcast_decoder_message(self, message):
        """Generic decoder message broadcast."""
        decoder_name = message.get("decoder", "unknown").upper()
        self._broadcast(json.dumps({"type": decoder_name, "message": message}))

    def _load_bookmarks(self):
        try:
            if BOOKMARKS_FILE.exists():
                data = json.loads(BOOKMARKS_FILE.read_text())
                return data if isinstance(data, (list, dict)) else []
            return []
        except Exception:
            return []

    def _save_bookmarks(self, data):
        try:
            BOOKMARKS_FILE.write_text(json.dumps(data, indent=2))
            self.bookmarks = data
            return True
        except Exception:
            return False

    def _default_connection_entry(self):
        return {
            "id": DEFAULT_CONNECTION_ID,
            "name": "Local RTL-TCP",
            "host": RTL_HOST,
            "port": RTL_PORT,
            "driver": "rtl_tcp",
            "sample_rate": SAMPLE_RATE,
        }

    def _load_connections(self):
        try:
            if CONNECTIONS_FILE.exists():
                data = json.loads(CONNECTIONS_FILE.read_text())
                entries = data.get("connections") if isinstance(data, dict) else data
                if isinstance(entries, list) and entries:
                    return entries
        except Exception as exc:
            logger.warning(f"Failed to load connections: {exc}")

        default = self._default_connection_entry()
        try:
            CONNECTIONS_FILE.write_text(json.dumps({"connections": [default]}, indent=2))
        except Exception:
            pass
        return [default]

    def _save_connections(self):
        with self._connections_lock:
            payload = {"connections": self.connections}
        try:
            CONNECTIONS_FILE.write_text(json.dumps(payload, indent=2))
            return True
        except Exception as exc:
            logger.warning(f"Failed to save connections: {exc}")
            return False

    def _drain_raw_queue(self):
        while not self._raw_queue.empty():
            try:
                self._raw_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

    def _broadcast_connection_state(self, connected, config=None, profile_id=None, reason=None):
        msg = {
            "type": "CONNECTION_CHANGED",
            "connected": connected,
            "profile_id": profile_id,
        }
        if config:
            msg.update({
                "host": config.get("host"),
                "port": config.get("port"),
                "driver": config.get("driver"),
                "sample_rate": config.get("sample_rate"),
                "name": config.get("name") or f"{config.get('host')}:{config.get('port')}",
            })
        if reason:
            msg["reason"] = reason
        self._broadcast(json.dumps(msg))

    async def _apply_connection_config(self, config):
        host = config.get("host", RTL_HOST)
        port = int(config.get("port", RTL_PORT))
        sample_rate = int(config.get("sample_rate", SAMPLE_RATE))
        self.rtl.host = host
        self.rtl.port = port
        self.rtl.sample_rate = sample_rate
        self._drain_raw_queue()
        await self.rtl.connect()
        await self.rtl.set_center_freq(DEFAULT_FREQ)
        self._broadcast(json.dumps({
            "type": "FREQ_CHANGED",
            "value": self.rtl.center_freq
        }))
        if self.dsp.sample_rate != sample_rate:
            with self._dsp_lock:
                self.dsp = RadioDSP(sample_rate=sample_rate, fft_size=FFT_SIZE)
                self.scanner.dsp = self.dsp

    async def _disconnect_hardware(self, config=None, profile_id=None, reason=None):
        if self.streaming:
            self.streaming = False
            self._broadcast(json.dumps({"type": "STREAM_STATE", "streaming": False}))
        if self.rtl.connected:
            try:
                await self.rtl.disconnect()
            except Exception:
                pass
        self._broadcast_connection_state(False, config, profile_id, reason or "disconnected")

    def _start_iq_recording(self):
        with self._recording_lock:
            if self.iq_recording:
                return
            fname = f"iq_{int(time.time())}.raw"
            fpath = RECORDINGS_DIR / fname
            self.iq_capture_file = open(fpath, "wb")
            self.iq_capture_filename = fname
            self.iq_recording = True
            audio_rec = self.audio_recording
        logger.info(f"IQ recording started: {fname}")
        self._broadcast(json.dumps({"type": "RECORD_STATUS", "iq": True, "iq_file": fname,
                                     "audio": audio_rec}))

    def _stop_iq_recording(self):
        with self._recording_lock:
            if not self.iq_recording:
                return
            fname = self.iq_capture_filename
            try:
                self.iq_capture_file.close()
            except Exception:
                pass
            self.iq_capture_file = None
            self.iq_recording = False
            self.iq_capture_filename = None
            audio_rec = self.audio_recording
        logger.info(f"IQ recording stopped: {fname}")
        self._broadcast(json.dumps({"type": "RECORD_STATUS", "iq": False, "audio": audio_rec}))

    def _start_audio_recording(self):
        with self._recording_lock:
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
            iq_rec = self.iq_recording
        logger.info(f"Audio recording started: {fname}")
        self._broadcast(json.dumps({"type": "RECORD_STATUS", "audio": True, "audio_file": fname,
                                     "iq": iq_rec}))

    def _stop_audio_recording(self):
        with self._recording_lock:
            if not self.audio_recording:
                return
            fname = self.audio_wav_filename
            try:
                self.audio_wav_file.close()
            except Exception:
                pass
            self.audio_wav_file = None
            self.audio_recording = False
            self.audio_wav_filename = None
            iq_rec = self.iq_recording
        logger.info(f"Audio recording stopped: {fname}")
        self._broadcast(json.dumps({"type": "RECORD_STATUS", "audio": False, "iq": iq_rec}))

    async def register(self, ws):
        queue = asyncio.Queue(maxsize=100)
        with self._clients_lock:
            self.clients[ws] = {"queue": queue, "audio": None}
            num_clients = len(self.clients)
        
        asyncio.create_task(self._client_sender(ws, queue))

        logger.info(f"Client connected ({num_clients} total)")
        await ws.send(json.dumps({
            "type": "STATE",
            "mode": self.dsp.mode,
            "squelch": self.dsp.squelch_threshold,
            "streaming": self.streaming,
            "freq": self.rtl.center_freq,
            "sample_rate": self.rtl.sample_rate,
            "rtl_host": self.rtl.host,
            "rtl_port": self.rtl.port,
            "fft_size": FFT_SIZE,
            "connected": self.rtl.connected,
            "connection_id": self._desired_connection_id,
            "connection_name": self._desired_connection["name"] if self._desired_connection else None,
            "connection_driver": self._desired_connection["driver"] if self._desired_connection else None,
            "connection_sample_rate": self.rtl.sample_rate,
            "iq_recording": self.iq_recording,
            "audio_recording": self.audio_recording,
        }))

    async def unregister(self, ws):
        with self._clients_lock:
            self.clients.pop(ws, None)
            num_clients = len(self.clients)
        logger.info(f"Client disconnected ({num_clients} total)")

    async def _client_sender(self, ws, queue):
        try:
            while True:
                msg = await queue.get()
                try:
                    await ws.send(msg)
                    # Check for pending audio message
                    with self._clients_lock:
                        client = self.clients.get(ws)
                        if client and client["audio"] is not None:
                            audio_msg = client["audio"]
                            client["audio"] = None
                        else:
                            audio_msg = None
                    if audio_msg:
                        await ws.send(audio_msg)
                except Exception:
                    break
        finally:
            await self.unregister(ws)

    def _broadcast(self, msg, audio=False):
        if not self._loop:
            return
        # Create snapshot to avoid RuntimeError during client connect/disconnect
        with self._clients_lock:
            clients_snapshot = list(self.clients.values())
        
        for client_info in clients_snapshot:
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
            if t == "CONNECT":
                host = msg.get("host", RTL_HOST)
                port = int(msg.get("port", RTL_PORT))
                sample_rate = int(msg.get("sample_rate", SAMPLE_RATE))
                driver = msg.get("driver", "rtl_tcp")
                name = msg.get("name") or f"{host}:{port}"
                profile_id = msg.get("profile_id") or uuid.uuid4().hex
                self._desired_connection = {
                    "host": host,
                    "port": port,
                    "driver": driver,
                    "sample_rate": sample_rate,
                    "name": name,
                }
                self._desired_connection_id = profile_id
                self._desired_connection_nonce += 1
                return
            elif t == "DISCONNECT":
                self._desired_connection = None
                self._desired_connection_id = None
                self._desired_connection_nonce += 1
                return
            if t == "START_STREAM":
                self.streaming = True
                self._broadcast(json.dumps({"type": "STREAM_STATE", "streaming": True}))
            elif t == "STOP_STREAM":
                self.streaming = False
                self._broadcast(json.dumps({"type": "STREAM_STATE", "streaming": False}))
            elif t == "SET_MODE":
                with self._dsp_lock:
                    self.dsp.set_mode(msg.get("mode", "FM"))
                    mode = self.dsp.mode
                self._broadcast(json.dumps({"type": "MODE_CHANGED", "mode": mode}))
            elif t == "SET_SQUELCH":
                with self._dsp_lock:
                    self.dsp.set_squelch(float(msg.get("value", -60)))
                    squelch = self.dsp.squelch_threshold
                self._broadcast(json.dumps({"type": "SQUELCH_CHANGED", "value": squelch}))
            elif t == "SET_FREQ":
                await self.rtl.set_center_freq(int(msg.get("value", 100000000)))
                self._broadcast(json.dumps({"type": "FREQ_CHANGED", "value": self.rtl.center_freq}))
            elif t == "SET_GAIN":
                await self.rtl.set_gain(int(msg.get("value", 400)))
            elif t == "SET_AGC":
                await self.rtl.set_agc(1 if msg.get("value") else 0)
            elif t == "START_SCAN":
                await self.scanner.start(category_name=msg.get("category"))
            elif t == "START_RANGE_SCAN":
                await self.scanner.start_range(
                    start_freq=int(msg.get("start", 88000000)),
                    end_freq=int(msg.get("end", 108000000)),
                    step=int(msg.get("step", 100000)),
                    mode=msg.get("mode", self.dsp.mode),
                )
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
                if self.pocsag:
                    self.pocsag.enabled = self.decode_pocsag
                logger.info(f"POCSAG decoder {'enabled' if self.decode_pocsag else 'disabled'}")
            elif t == "TOGGLE_DECODER":
                name = msg.get("name", "")
                enabled = bool(msg.get("value", False))
                if name in self.decoders:
                    self.decoders[name].enabled = enabled
                    if name == "pocsag":
                        self.decode_pocsag = enabled
                    logger.info(f"Decoder '{name}' {'enabled' if enabled else 'disabled'}")
                    self._broadcast(json.dumps({"type": "DECODER_STATE", "name": name, "enabled": enabled}))
            elif t == "LIST_DECODERS":
                infos = [d.info() for d in self.decoders.values()]
                try:
                    await ws.send(json.dumps({"type": "DECODER_LIST", "decoders": infos}))
                except Exception:
                    pass
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

    def _process_chunk(self, data, streaming, dsp, dsp_lock, decoders, decode_enabled,
                       iq_recording, iq_file, audio_recording, audio_wav, recording_lock):
        """Run in thread pool. Thread-safe with lock protection."""
        # IQ recording (file handle protected by recording_lock)
        if iq_recording and iq_file:
            with recording_lock:
                try:
                    if iq_file:  # Re-check after acquiring lock
                        iq_file.write(data)
                except Exception:
                    pass

        raw = np.frombuffer(data, dtype=np.uint8).astype(np.float32)
        raw = (raw - 127.5) / 127.5
        iq = raw[0::2] + 1j * raw[1::2]
        
        # DSP operations protected by lock (dsp state can change from main thread)
        with dsp_lock:
            fft = dsp.compute_fft(iq)

        # Check if any decoder needs audio or IQ
        any_audio_decoder = any(
            d.enabled and d.input_type.name == "AUDIO" for d in decoders.values()
        ) if decode_enabled else False
        any_iq_decoder = any(
            d.enabled and d.input_type.name == "IQ" for d in decoders.values()
        ) if decode_enabled else False

        # Demodulate if streaming, decoding (audio type), or audio recording
        should_demod = streaming or any_audio_decoder or audio_recording
        audio = None
        if should_demod:
            with dsp_lock:
                audio = dsp.demodulate(iq)

        # Feed enabled decoders
        if decode_enabled:
            for dec in decoders.values():
                if not dec.enabled:
                    continue
                try:
                    if dec.input_type.name == "AUDIO" and audio is not None:
                        dec.process_audio(audio)
                    elif dec.input_type.name == "IQ":
                        dec.process_iq(iq)
                except Exception:
                    logger.exception(f"Decoder '{dec.name}' error")

        # Audio recording — write demodulated audio as 16-bit PCM to WAV
        if audio_recording and audio_wav and audio is not None and len(audio) > 0:
            with recording_lock:
                try:
                    if audio_wav:  # Re-check after acquiring lock
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
                    data, self.streaming, self.dsp, self._dsp_lock, self.decoders,
                    any(d.enabled for d in self.decoders.values()),
                    self.iq_recording, self.iq_capture_file,
                    self.audio_recording, self.audio_wav_file, self._recording_lock
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

    async def _connection_manager_loop(self):
        active_config = None
        active_profile = None
        active_nonce = -1
        while self.running:
            desired = self._desired_connection
            desired_nonce = self._desired_connection_nonce
            if not desired:
                if active_config is not None:
                    await self._disconnect_hardware(active_config, active_profile, reason="requested disconnect")
                    active_config = None
                    active_profile = None
                    active_nonce = desired_nonce
                await asyncio.sleep(0.1)
                continue
            if active_config and desired_nonce == active_nonce:
                await asyncio.sleep(0.1)
                continue
            if active_config:
                await self._disconnect_hardware(active_config, active_profile, reason="switching connection")
                active_config = None
                active_profile = None
            try:
                await self._apply_connection_config(desired)
                active_config = dict(desired)
                active_profile = self._desired_connection_id
                active_nonce = desired_nonce
                self._broadcast_connection_state(True, config=active_config, profile_id=active_profile, reason="connected")
            except Exception as exc:
                logger.warning(f"Connection manager error: {exc}")
                self._broadcast_connection_state(False, config=desired, profile_id=self._desired_connection_id, reason=str(exc))
                await asyncio.sleep(2)

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
                            writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nAccess-Control-Allow-Origin: *\r\nContent-Length: 11\r\n\r\n{"ok":true}')
                        else:
                            writer.write(b'HTTP/1.1 500 Error\r\nAccess-Control-Allow-Origin: *\r\nContent-Length: 12\r\n\r\n{"ok":false}')
                    except Exception:
                        writer.write(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
                elif method == "OPTIONS":
                    writer.write(b"HTTP/1.1 204 No Content\r\nAccess-Control-Allow-Origin: *\r\nAccess-Control-Allow-Methods: GET, POST, OPTIONS\r\nAccess-Control-Allow-Headers: Content-Type\r\n\r\n")
            elif path == "/api/connections":
                if method == "GET":
                    payload = {
                        "connections": self.connections,
                        "selected_id": self._desired_connection_id,
                        "connected": self.rtl.connected,
                        "connection_name": (self._desired_connection.get("name") if self._desired_connection else None),
                        "connection_driver": (self._desired_connection.get("driver") if self._desired_connection else None),
                        "connection_sample_rate": (self._desired_connection.get("sample_rate") if self._desired_connection else None),
                        "connection_host": (self._desired_connection.get("host") if self._desired_connection else None),
                        "connection_port": (self._desired_connection.get("port") if self._desired_connection else None),
                    }
                    body = json.dumps(payload).encode()
                    writer.write(f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\nAccess-Control-Allow-Origin: *\r\n\r\n".encode() + body)
                elif method == "POST":
                    try:
                        body_part = req.split(b"\r\n\r\n", 1)[1]
                        data = json.loads(body_part.decode())
                        entries = data.get("connections")
                        if not isinstance(entries, list):
                            raise ValueError("connections must be a list")
                        sanitized = []
                        for entry in entries:
                            host = entry.get("host")
                            port = int(entry.get("port", 0))
                            if not host or port <= 0:
                                continue
                            driver = entry.get("driver", "rtl_tcp")
                            sample_rate = int(entry.get("sample_rate", SAMPLE_RATE))
                            sanitized.append({
                                "id": entry.get("id") or uuid.uuid4().hex,
                                "name": entry.get("name") or f"{host}:{port}",
                                "host": host,
                                "port": port,
                                "driver": driver,
                                "sample_rate": sample_rate,
                            })
                        with self._connections_lock:
                            self.connections = sanitized
                        if self._save_connections():
                            writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nAccess-Control-Allow-Origin: *\r\nContent-Length: 11\r\n\r\n{"ok":true}')
                        else:
                            writer.write(b'HTTP/1.1 500 Error\r\nAccess-Control-Allow-Origin: *\r\nContent-Length: 12\r\n\r\n{"ok":false}')
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
        self._loop = asyncio.get_running_loop()
        asyncio.create_task(self.reader_loop())
        asyncio.create_task(self.processor_loop())
        asyncio.create_task(self._connection_manager_loop())
        async with serve(self.ws_handler, WS_HOST, WS_PORT):
            http_server = await asyncio.start_server(self.http_handler, WS_HOST, HTTP_PORT)
            logger.info(f"HTTP serving {FRONTEND_DIR} on :{HTTP_PORT}")
            await http_server.serve_forever()


if __name__ == "__main__":
    asyncio.run(SDRServer().run())
