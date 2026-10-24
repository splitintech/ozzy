import re

from voice_studio_core.contracts import DomainError


def segments(text, max_chars=180):
    """Bound long messages without dropping punctuation, words, or final fragments."""
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        raise DomainError("empty_script", "Enter a script", 422)
    sentences = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\"'])", text)
    result = []
    current = ""
    for sentence in sentences:
        for word in sentence.split():
            if len(word) > max_chars:
                raise DomainError("word_too_long", "A word exceeds the model input limit", 422)
            candidate = f"{current} {word}".strip()
            if len(candidate) > max_chars:
                result.append(current)
                current = word
            else:
                current = candidate
        if current:
            result.append(current)
            current = ""
    if " ".join(result) != text:
        raise RuntimeError("Segmentation changed the script")
    return result
