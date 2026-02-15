"""Base Decoder class for evilSDR plugin architecture.

All signal decoders should subclass BaseDecoder and implement the required methods.
Decoders are automatically discovered and loaded from the decoders/ directory.
"""

from abc import ABC, abstractmethod
from enum import Enum, auto
from typing import Any, Callable, Optional
import logging
import numpy as np

logger = logging.getLogger(__name__)


class InputType(Enum):
    """Types of input a decoder can accept."""
    AUDIO = auto()   # FM-demodulated audio samples (float32)
    IQ = auto()      # Raw IQ samples (complex64)


class DecoderState(Enum):
    """Runtime state of a decoder."""
    IDLE = "idle"
    RUNNING = "running"
    ERROR = "error"


class BaseDecoder(ABC):
    """Abstract base class for all signal decoders.

    Subclasses must:
      - Set `name`, `description`, and `input_type` class attributes.
      - Implement `process_audio()` and/or `process_iq()` depending on `input_type`.
      - Implement `get_history()` to return recently decoded messages.
      - Implement `reset()` to clear internal state.

    The `emit()` helper pushes decoded data to all registered callbacks.
    """

    # --- Class attributes (override in subclass) ---
    name: str = "base"
    description: str = "Abstract base decoder"
    input_type: InputType = InputType.AUDIO

    def __init__(self, sample_rate: int = 48000):
        self.sample_rate = sample_rate
        self._callbacks: list[Callable[[dict], Any]] = []
        self.state: DecoderState = DecoderState.IDLE
        self._enabled: bool = False

    # --- Callback management ---

    def add_callback(self, callback: Callable[[dict], Any]):
        """Register a callback that receives decoded messages (dicts)."""
        if callback not in self._callbacks:
            self._callbacks.append(callback)

    def remove_callback(self, callback: Callable[[dict], Any]):
        """Unregister a callback."""
        try:
            self._callbacks.remove(callback)
        except ValueError:
            pass

    def set_callback(self, callback: Optional[Callable[[dict], Any]]):
        """Convenience: set a single callback (clears previous ones)."""
        self._callbacks.clear()
        if callback is not None:
            self._callbacks.append(callback)

    def emit(self, message: dict):
        """Push a decoded message dict to all registered callbacks."""
        message.setdefault("decoder", self.name)
        for cb in self._callbacks:
            try:
                cb(message)
            except Exception:
                logger.exception(f"Callback error in decoder '{self.name}'")

    # --- Enable / disable ---

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool):
        self._enabled = value
        self.state = DecoderState.RUNNING if value else DecoderState.IDLE

    # --- Input processing ---

    def process_audio(self, samples: np.ndarray):
        """Process FM-demodulated audio samples (float32).

        Override this if input_type includes AUDIO.
        Default implementation is a no-op.
        """
        pass

    def process_iq(self, iq_samples: np.ndarray):
        """Process raw IQ samples (complex64).

        Override this if input_type is IQ.
        Default implementation is a no-op.
        """
        pass

    # --- Required overrides ---

    @abstractmethod
    def get_history(self, limit: int = 50) -> list[dict]:
        """Return recent decoded messages as a list of dicts."""
        ...

    @abstractmethod
    def reset(self):
        """Clear internal buffers and state."""
        ...

    # --- Info ---

    def info(self) -> dict:
        """Return metadata about this decoder for the UI."""
        return {
            "name": self.name,
            "description": self.description,
            "input_type": self.input_type.name.lower(),
            "enabled": self.enabled,
            "state": self.state.value,
        }

    def __repr__(self):
        return f"<{self.__class__.__name__} name={self.name!r} enabled={self.enabled}>"
