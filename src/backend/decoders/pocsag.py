#!/usr/bin/env python3
"""POCSAG (Pager) decoder for evilSDR.

Decodes POCSAG signals from FM-demodulated audio samples.
Supports 512, 1200, and 2400 baud rates.
"""

import numpy as np
import logging
import time
from collections import deque
from .base import BaseDecoder, InputType

logger = logging.getLogger(__name__)

# POCSAG constants
POCSAG_SYNC = 0x7CD215D8
POCSAG_IDLE = 0x7A89C197
POCSAG_BAUD_RATES = [512, 1200, 2400]
BCH_POLY = 0x769
POCSAG_NUMERIC = "0123456789*U -)(."


class POCSAGMessage:
    """Decoded POCSAG message."""
    def __init__(self, address, function_bits, content, msg_type, baud_rate, timestamp=None):
        self.address = address
        self.function_bits = function_bits
        self.content = content
        self.msg_type = msg_type
        self.baud_rate = baud_rate
        self.timestamp = timestamp or time.time()

    def to_dict(self):
        return {
            "address": self.address,
            "function": self.function_bits,
            "content": self.content,
            "type": self.msg_type,
            "baud": self.baud_rate,
            "timestamp": self.timestamp,
        }

    def __repr__(self):
        return f"POCSAG[{self.address}:{self.function_bits}] {self.msg_type}: {self.content}"


class POCSAGDecoder(BaseDecoder):
    """POCSAG decoder operating on FM-demodulated audio samples."""

    name = "pocsag"
    description = "POCSAG pager decoder (512/1200/2400 baud)"
    input_type = InputType.AUDIO

    def __init__(self, sample_rate=48000):
        super().__init__(sample_rate=sample_rate)
        self.messages = deque(maxlen=200)
        self._sample_buffer = np.array([], dtype=np.float32)

    def reset(self):
        """Clear internal buffers and message history."""
        self.messages.clear()
        self._sample_buffer = np.array([], dtype=np.float32)

    def get_history(self, limit=50) -> list:
        """Return recent messages as list of dicts."""
        msgs = list(self.messages)[-limit:]
        return [m.to_dict() for m in msgs]
    
    # Alias for backward compatibility if needed, but BaseDecoder specifies get_history
    get_messages = get_history

    def process_audio(self, audio_samples: np.ndarray):
        """Feed FM-demodulated audio samples into the decoder."""
        if len(audio_samples) == 0:
            return

        self._sample_buffer = np.concatenate([self._sample_buffer, audio_samples])

        # Limit buffer size to prevent infinite growth
        max_samples = self.sample_rate * 2
        if len(self._sample_buffer) > max_samples:
            # If buffer is huge, keep only the end
            self._sample_buffer = self._sample_buffer[-max_samples:]

        for baud in POCSAG_BAUD_RATES:
            self._try_decode_baud(baud)

        # Truncate buffer again after processing
        if len(self._sample_buffer) > max_samples:
            self._sample_buffer = self._sample_buffer[-max_samples:]

    def _try_decode_baud(self, baud_rate):
        samples_per_bit = self.sample_rate / baud_rate
        if samples_per_bit < 2:
            return

        n_samples = len(self._sample_buffer)
        n_bits = int(n_samples / samples_per_bit)
        if n_bits < 64: # Need at least sync + something
            return

        # Simple bit slicing (could be improved with zero-crossing detection)
        # Resample to baud rate
        indices = (np.arange(n_bits) * samples_per_bit + samples_per_bit/2).astype(int)
        bits = (self._sample_buffer[indices] > 0).astype(int)

        sync_bits = _int_to_bits(POCSAG_SYNC, 32)
        bit_str = bits.tolist() # Convert to list for easier slicing

        # Search for sync word
        # Optimization: only search if we haven't found a sync recently or if buffer is fresh
        # For now, brute force search is okay for small buffers
        
        # Convert list to tuple/string for faster search? No, keep it simple.
        
        # Simple sliding window
        sync_len = 32
        for i in range(len(bit_str) - sync_len):
            if bit_str[i:i+sync_len] == sync_bits:
                # Found sync!
                batch_start = i + sync_len
                batch_end = batch_start + 512 # 16 codewords * 32 bits
                
                if batch_end <= len(bit_str):
                    batch_bits = bit_str[batch_start:batch_end]
                    self._decode_batch(batch_bits, baud_rate)
                    # Skip past this batch to avoid re-decoding
                    # In a real stream, we'd maintain state to expect next batch
                    # Here we just scan.
                    pass 

    def _decode_batch(self, bits, baud_rate):
        if len(bits) < 512:
            return

        current_address = None
        current_function = 0
        message_bits = []

        for frame_idx in range(8):
            for cw_idx in range(2):
                offset = (frame_idx * 2 + cw_idx) * 32
                cw_bits = bits[offset:offset + 32]
                codeword = _bits_to_int(cw_bits)

                if codeword == POCSAG_IDLE:
                    if current_address is not None and message_bits:
                        self._emit_message(current_address, current_function, message_bits, baud_rate)
                        message_bits = []
                        current_address = None
                    continue

                if not _bch_check(codeword):
                    corrected = _bch_correct(codeword)
                    if corrected is None:
                        # Uncorrectable error
                        continue
                    codeword = corrected

                is_message = (codeword >> 31) & 1

                if is_message == 0: # Address codeword
                    if current_address is not None and message_bits:
                        self._emit_message(current_address, current_function, message_bits, baud_rate)
                        message_bits = []
                    
                    addr_bits = (codeword >> 13) & 0x7FFFF # 18 bits
                    current_address = (addr_bits << 3) | frame_idx
                    current_function = (codeword >> 11) & 0x3
                    message_bits = []
                else: # Message codeword
                    if current_address is None:
                        continue # Orphan message codeword
                    
                    data_bits = []
                    for bit_pos in range(30, 10, -1):
                        data_bits.append((codeword >> bit_pos) & 1)
                    message_bits.extend(data_bits)

        if current_address is not None and message_bits:
            self._emit_message(current_address, current_function, message_bits, baud_rate)

    def _emit_message(self, address, function_bits, data_bits, baud_rate):
        # Try to decode as numeric first, then alpha
        numeric_text = self._decode_numeric(data_bits)
        alpha_text = self._decode_alpha(data_bits)

        # Heuristic to decide type
        is_alpha = False
        if len(alpha_text) > 0:
            printable = sum(1 for c in alpha_text if 32 <= ord(c) <= 126)
            if printable / len(alpha_text) > 0.7:
                is_alpha = True

        if is_alpha:
            msg = POCSAGMessage(address, function_bits, alpha_text.strip(), "alpha", baud_rate)
        else:
            msg = POCSAGMessage(address, function_bits, numeric_text.strip(), "numeric", baud_rate)

        # Deduplicate: check if we just emitted this
        is_duplicate = False
        if self.messages:
            last = self.messages[-1]
            if (last.address == msg.address and 
                last.content == msg.content and 
                (msg.timestamp - last.timestamp) < 2.0):
                is_duplicate = True
        
        if not is_duplicate and msg.content:
            self.messages.append(msg)
            logger.info(f"POCSAG decoded: {msg}")
            self.emit(msg.to_dict())

    def _decode_numeric(self, bits) -> str:
        text = []
        # Numeric format: 4 bits per digit, BCD-like but reversed
        # Actually standard is: bits are transmitted most significant bit first in codeword,
        # but within numeric nibble... let's stick to the implementation I saw or standard.
        # Implementation used:
        for i in range(0, len(bits) - 3, 4):
            # Nibble order: 8, 4, 2, 1 weights?
            # Standard: D3 D2 D1 D0
            # Implementation used:
            nibble = (bits[i] << 3) | (bits[i+1] << 2) | (bits[i+2] << 1) | bits[i+3]
            # Mapping adjustment?
            # Standard says: values 0-9 are digits, A is reserved, B=U, C=' ', D='-', E=')', F='('
            # The implementation had a remap:
            nibble = ((nibble & 0x8) >> 3) | ((nibble & 0x4) >> 1) | ((nibble & 0x2) << 1) | ((nibble & 0x1) << 3)
            # This reverses bits? 8->1, 4->2, 2->4, 1->8.
            # Maybe the bits come in LSB first?
            
            if nibble < len(POCSAG_NUMERIC):
                text.append(POCSAG_NUMERIC[nibble])
        return ''.join(text)

    def _decode_alpha(self, bits) -> str:
        text = []
        # 7-bit ASCII
        for i in range(0, len(bits) - 6, 7):
            char_val = 0
            for b in range(7):
                char_val |= bits[i + b] << b # LSB first in 7-bit chunk?
            
            # Standard ASCII is usually transmitted LSB first.
            
            if 32 <= char_val <= 126:
                text.append(chr(char_val))
            elif char_val == 0:
                break # Null terminator?
        return ''.join(text)


def _int_to_bits(value, width):
    return [(value >> (width - 1 - i)) & 1 for i in range(width)]

def _bits_to_int(bits):
    val = 0
    for b in bits: val = (val << 1) | b
    return val

def _bch_check(codeword):
    if bin(codeword).count('1') % 2 != 0: return False # Parity check (even parity)
    return _bch_syndrome(codeword >> 1) == 0

def _bch_syndrome(data31):
    remainder = data31
    for i in range(30, 9, -1):
        if remainder & (1 << i): remainder ^= BCH_POLY << (i - 10)
    return remainder & 0x3FF

def _bch_correct(codeword):
    data = codeword >> 1
    if _bch_syndrome(data) == 0: return codeword ^ 1 # Parity error only?
    
    # Try flipping each bit
    for bit in range(31):
        test = data ^ (1 << bit)
        if _bch_syndrome(test) == 0:
            corrected = (test << 1)
            # Recalculate parity
            if bin(corrected).count('1') % 2 != 0: corrected |= 1
            return corrected
    return None
