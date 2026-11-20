import asyncio
from collections.abc import AsyncIterator, Callable

import azure.cognitiveservices.speech as speechsdk

from app.config import settings

PHRASES = ["Amoxicillin", "Atorvastatin", "Metformin", "Omeprazole", "Salbutamol",
           "Dr. Patel", "Dr. Smith", "Alex Demo", "repeat prescription", "NHS"]


class AzureVoiceBridge:
    def __init__(
        self, on_interim: Callable[[str], None], on_final: Callable[[str], None],
        on_error: Callable[[str], None],
    ):
        if not settings.AZURE_SPEECH_KEY:
            raise ValueError("Set AZURE_SPEECH_KEY for voice mode; text mode is still available")
        self.loop = asyncio.get_running_loop()
        self.active = True
        self.config = speechsdk.SpeechConfig(
            subscription=settings.AZURE_SPEECH_KEY, region=settings.AZURE_SPEECH_REGION,
        )
        self.config.speech_recognition_language = settings.AZURE_SPEECH_LOCALE
        self.config.speech_synthesis_voice_name = settings.AZURE_TTS_VOICE
        self.config.set_speech_synthesis_output_format(
            speechsdk.SpeechSynthesisOutputFormat.Raw16Khz16BitMonoPcm
        )
        self.config.set_property(speechsdk.PropertyId.SpeechServiceConnection_EndSilenceTimeoutMs, "650")
        self.push = speechsdk.audio.PushAudioInputStream(
            stream_format=speechsdk.audio.AudioStreamFormat(
                samples_per_second=16000, bits_per_sample=16, channels=1
            )
        )
        self.recognizer = speechsdk.SpeechRecognizer(
            speech_config=self.config, audio_config=speechsdk.audio.AudioConfig(stream=self.push)
        )
        grammar = speechsdk.PhraseListGrammar.from_recognizer(self.recognizer)
        for phrase in PHRASES:
            grammar.addPhrase(phrase)
        def dispatch(callback: Callable[[str], None], text: str) -> None:
            if self.active:
                self.loop.call_soon_threadsafe(callback, text)
        self.recognizer.recognizing.connect(lambda evt: dispatch(on_interim, evt.result.text))
        self.recognizer.recognized.connect(
            lambda evt: dispatch(on_final, evt.result.text)
            if evt.result.reason == speechsdk.ResultReason.RecognizedSpeech else None
        )
        self.recognizer.canceled.connect(
            lambda evt: dispatch(on_error, f"Speech recognition canceled: {evt.reason}; {evt.error_details}")
            if evt.reason == speechsdk.CancellationReason.Error else None
        )

    async def start(self) -> None:
        await asyncio.to_thread(lambda: self.recognizer.start_continuous_recognition_async().get())

    def write(self, data: bytes) -> None:
        self.push.write(data)

    async def close(self) -> None:
        self.active = False
        self.push.close()
        await asyncio.to_thread(lambda: self.recognizer.stop_continuous_recognition_async().get())

    async def synthesize(self, sentence: str) -> AsyncIterator[bytes]:
        synthesizer = speechsdk.SpeechSynthesizer(speech_config=self.config, audio_config=None)
        queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        accepting = True
        def on_audio(evt) -> None:
            if accepting and evt.result.audio_data:
                self.loop.call_soon_threadsafe(queue.put_nowait, bytes(evt.result.audio_data))
        synthesizer.synthesizing.connect(on_audio)

        async def produce():
            try:
                result = await asyncio.to_thread(lambda: synthesizer.speak_text_async(sentence).get())
                if result.reason != speechsdk.ResultReason.SynthesizingAudioCompleted:
                    details = result.cancellation_details
                    raise RuntimeError(f"Speech synthesis failed: {details.reason}; {details.error_details}")
            finally:
                queue.put_nowait(None)
        worker = asyncio.create_task(produce())
        try:
            while (data := await queue.get()) is not None:
                yield data
            await worker
        finally:
            accepting = False
            if not worker.done():
                await asyncio.to_thread(lambda: synthesizer.stop_speaking_async().get())
                await worker
            synthesizer.synthesizing.disconnect_all()
