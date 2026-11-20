import re
from collections.abc import AsyncIterator

BOUNDARY = re.compile(r"(?<=[.!?])\s+")
ABBREVIATION = re.compile(r"\b(?:Dr|Mr|Mrs|Ms|St|Prof)\.$", re.I)


async def stream_sentences_from_tokens(tokens: AsyncIterator[str]) -> AsyncIterator[str]:
    buffer = ""
    async for token in tokens:
        buffer += token
        while match := BOUNDARY.search(buffer):
            sentence = buffer[:match.start()].strip()
            if ABBREVIATION.search(sentence):
                following = BOUNDARY.search(buffer, match.end())
                if not following:
                    break
                match = following
                sentence = buffer[:match.start()].strip()
            if sentence:
                yield sentence
            buffer = buffer[match.end():]
    if buffer.strip():
        yield buffer.strip()
