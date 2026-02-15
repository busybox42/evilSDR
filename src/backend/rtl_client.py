#!/usr/bin/env python3
"""RTL-TCP client - connects to rtl_tcp server and provides IQ samples."""

import asyncio
import struct
import logging
import numpy as np

logger = logging.getLogger(__name__)

CMD_SET_FREQ = 0x01
CMD_SET_SAMPLE_RATE = 0x02
CMD_SET_GAIN_MODE = 0x03
CMD_SET_GAIN = 0x04
CMD_SET_AGC = 0x08


class RTLTCPClient:
    def __init__(self, host="127.0.0.1", port=1234):
        self.host = host
        self.port = port
        self.reader = None
        self.writer = None
        self.connected = False
        self.tuner_type = "unknown"
        self.gain_count = 0
        self.sample_rate = 2_400_000
        self.center_freq = 100_000_000

    async def connect(self):
        self.reader, self.writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port), timeout=5
        )
        data = await asyncio.wait_for(self.reader.readexactly(12), timeout=5)
        if data[:4] != b"RTL0":
            raise ConnectionError(f"Invalid rtl_tcp magic: {data[:4]}")

        tuner_type = struct.unpack(">I", data[4:8])[0]
        self.gain_count = struct.unpack(">I", data[8:12])[0]
        tuner_names = {1: "E4000", 2: "FC0012", 3: "FC0013",
                       4: "FC2580", 5: "R820T", 6: "R828D"}
        self.tuner_type = tuner_names.get(tuner_type, f"unknown({tuner_type})")
        self.connected = True
        logger.info(f"Connected: tuner={self.tuner_type}, gains={self.gain_count}")

        await self.set_sample_rate(self.sample_rate)
        await self.set_center_freq(self.center_freq)
        await self.set_gain_mode(1)
        await self.set_gain(400)

    async def _send_cmd(self, cmd_id: int, param: int):
        if self.writer:
            self.writer.write(struct.pack(">BI", cmd_id, param))
            await self.writer.drain()

    async def set_center_freq(self, freq: int):
        self.center_freq = freq
        await self._send_cmd(CMD_SET_FREQ, freq)

    async def set_sample_rate(self, rate: int):
        self.sample_rate = rate
        await self._send_cmd(CMD_SET_SAMPLE_RATE, rate)

    async def set_gain_mode(self, manual: int):
        await self._send_cmd(CMD_SET_GAIN_MODE, manual)

    async def set_gain(self, gain_tenths: int):
        await self._send_cmd(CMD_SET_GAIN, gain_tenths)

    async def set_agc(self, enabled: int):
        await self._send_cmd(CMD_SET_AGC, enabled)

    async def disconnect(self):
        if self.writer:
            self.writer.close()
            try:
                await self.writer.wait_closed()
            except Exception:
                pass
        self.connected = False
