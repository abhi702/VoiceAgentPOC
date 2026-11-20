import re
from dataclasses import dataclass

PATTERNS = {
    "chest_pain": r"\b(chest pain|crushing pain|tightness in (my |the )?chest|heart attack)\b",
    "stroke": r"\b(face droop\w*|slurred speech|arm weakness|stroke|one side numb)\b",
    "breathing": r"\b(can['’]?t breathe|cannot breathe|gasping for air|choking|lips turning blue)\b",
    "crisis": r"\b(uncontrolled bleeding|coughing up blood|unconscious|seizure|overdos\w*|suicid\w*|kill myself)\b",
}
COMPILED = [(name, re.compile(pattern, re.I)) for name, pattern in PATTERNS.items()]
EMERGENCY_SCRIPT = (
    "This may be an emergency; I cannot assess it or book a routine appointment. "
    "Call 999 now for urgent help."
)


@dataclass(frozen=True)
class GuardrailResult:
    triggered: bool
    category: str | None = None
    spoken_override: str | None = None


def evaluate_clinical_red_flags(text: str) -> GuardrailResult:
    normalized = re.sub(r"\s+", " ", text)
    for category, pattern in COMPILED:
        if pattern.search(normalized):
            return GuardrailResult(True, category, EMERGENCY_SCRIPT)
    return GuardrailResult(False)
