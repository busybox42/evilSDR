# evilSDR Decoder Plugin Interface

This is the minimal contract any new decoder plugin must satisfy so it can slot into the phase‑4 decoder pipeline without touching the rest of the stack.

## Anatomy

* **Base class:** `backend.decoders.base.BaseDecoder` (exported by `backend.decoders`).
* **Discovery:** `backend.decoders.__init__.discover_decoders()` scans `backend/decoders/` for any concrete `BaseDecoder` subclasses and uses `load_decoders()` to instantiate them with the current sample rate.
* **Input types:** The core accepts either `InputType.AUDIO` (FM demodulated audio) or `InputType.IQ` (raw complex samples). Set `input_type` accordingly.
* **Callbacks:** Plugins emit decoded results via `self.emit(dict)` and can register UI/output callbacks when the scanner or server starts them.

## Minimal skeleton

```python
from backend.decoders.base import BaseDecoder, InputType
import numpy as np

class TemplateDecoder(BaseDecoder):
    """Minimal plugin entry point for testing. """

    name = "template"
    description = "Example decoder that echoes peak values."
    input_type = InputType.AUDIO

    def __init__(self, sample_rate: int = 48000):
        super().__init__(sample_rate=sample_rate)
        self._history: list[dict] = []

    def process_audio(self, samples: np.ndarray):
        peak = float(np.max(np.abs(samples))) if samples.size else 0.0
        payload = {
            "peak": peak,
            "timestamp": self.sample_rate and len(samples) / self.sample_rate,
        }
        self._history.append(payload)
        self.emit(payload)

    def get_history(self, limit: int = 50) -> list[dict]:
        return self._history[-limit:]

    def reset(self):
        self._history.clear()
```

## Hooking into the server

1. The backend `server.py` should call `load_decoders()` once during startup and pass the resulting instances to whichever component is responsible for streaming audio/IQ.
2. Each decoder can attach callbacks that push messages into the websocket feed or write to log files.
3. Keep decoder state idempotent: `reset()` is invoked whenever the scanner stops or the user flips modes.

Drop new plugins under `backend/decoders/`. They will be auto-discovered (just avoid `template`/test helpers if you don’t want them loaded in production).