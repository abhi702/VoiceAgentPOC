class SileroDetector:
    def __init__(self) -> None:
        import torch
        from silero_vad import load_silero_vad

        self.torch = torch
        self.model = load_silero_vad()
        self.buffer = bytearray()
        self.frames = 0

    def speech_started(self, pcm: bytes) -> bool:
        self.buffer.extend(pcm)
        started = False
        while len(self.buffer) >= 1024:
            frame = bytes(self.buffer[:1024])
            del self.buffer[:1024]
            values = self.torch.frombuffer(bytearray(frame), dtype=self.torch.int16).float() / 32768
            with self.torch.no_grad():
                probability = self.model(values, 16000).item()
            self.frames = self.frames + 1 if probability > 0.65 else 0
            if self.frames == 3:
                started = True
        return started
