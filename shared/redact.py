"""Remove secrets from text (CI logs, commit messages) before an LLM or the database
sees it. Errs on the side of masking: a masked non-secret costs nothing."""
import re

_PATTERNS = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL),
     "[REDACTED PRIVATE KEY]"),
    (re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"), "[REDACTED GITHUB TOKEN]"),
    (re.compile(r"\b(xai-[A-Za-z0-9]{20,}|sk-[A-Za-z0-9_\-]{20,}|hf_[A-Za-z0-9]{20,})"), "[REDACTED API KEY]"),
    (re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b"), "[REDACTED AWS KEY]"),
    (re.compile(r"(?i)(authorization:\s*(bearer|token|basic)\s+)\S+"), r"\1[REDACTED]"),
    (re.compile(r"(?i)\b([a-z0-9_]*(password|passwd|secret|token|api_key|apikey)[a-z0-9_]*\s*[=:]\s*)"
                r"(\"[^\"]*\"|'[^']*'|\S+)"), r"\1[REDACTED]"),
    (re.compile(r"(?i)(postgres(ql)?://[^:/\s]+:)[^@\s]+@"), r"\1[REDACTED]@"),
]


def redact(text: str) -> str:
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def tail(text: str, max_lines: int = 300, max_chars: int = 20000) -> str:
    """Keep the end of a long log (where failures are reported)."""
    lines = text.splitlines()
    if len(lines) > max_lines:
        lines = [f"...[{len(lines) - max_lines} earlier lines cut]..."] + lines[-max_lines:]
    out = "\n".join(lines)
    return out[-max_chars:] if len(out) > max_chars else out
