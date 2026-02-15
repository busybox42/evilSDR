from abc import ABC, abstractmethod
import numpy as np

class BaseDecoder(ABC):
    """Abstract base class for all signal decoders."""
    
    def __init__(self, sample_rate: int = 48000):
        self.sample_rate = sample_rate
        self.callback = None

    def set_callback(self, callback):
        """Set callback function for decoded messages."""
        self.callback = callback

    def emit(self, message):
        """Emit a decoded message via callback."""
        if self.callback:
            self.callback(message)

    @abstractmethod
    def process_audio(self, samples: np.ndarray):
        """Process a chunk of audio samples."""
        pass

    @abstractmethod
    def get_history(self) -> list:
        """Return list of recent decoded messages."""
        pass
