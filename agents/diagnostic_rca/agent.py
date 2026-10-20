import json, os, re, sys, uuid
from pathlib import Path

from dotenv import load_dotenv

from agents.diagnostic_rca.retrieval import retrieve
from shared.events import publish_and_log
from shared.llm import LLMUnavailable, for_agent

load_dotenv()
BASE = Path(__file__).parent
DEFAULT_LOG = BASE / "logs/sample_failed_build.log"
MAX_LOG_CHARS = 3000
MAX_CAUSES = 3
MIN_CONFIDENCE = 0.2  # drop weak, usually unsupported causes (always keep at least one)
ERROR_PATTERN = re.compile(
    r"error|fail|exception|traceback|denied|refused|timed? ?out|not found|cannot|"
    r"unable|fatal|exit code|warning|ERESOLVE|unhealthy", re.I)


def clean_log(text: str) -> str:
    text = re.sub(r"\x1b\[[0-9;]*m", "", text)  # strip ANSI color codes
    text = re.sub(r"(?m)^\s*\d{4}-\d{2}-\d{2}T[\d:.]+Z\s*", "", text)  # strip CI timestamps (embedding/prompt noise)
    return "\n".join(line.rstrip() for line in text.splitlines() if line.strip())


def extract_error_context(log: str, max_chars: int = MAX_LOG_CHARS) -> str:
    """Keep error-looking lines plus a little surrounding context, so a long
    build log fits in a small model's context window."""
    lines = log.splitlines()
    keep: set[int] = set()
    for i, line in enumerate(lines):
        if ERROR_PATTERN.search(line):
            keep.update(range(max(0, i - 1), min(len(lines), i + 3)))
    excerpt = "\n".join(lines[i] for i in sorted(keep)) if keep else log
    if len(excerpt) > max_chars:  # keep the start (root cause) and the end (failing step)
        head, tail = max_chars // 3, 2 * max_chars // 3
        excerpt = excerpt[:head] + "\n...[truncated]...\n" + excerpt[-tail:]
    return excerpt


def build_queries(error_context: str, max_queries: int = 6) -> list[str]:
    """One short query per distinct error/warning line, so a specific doc
    (e.g. about a missing env var) isn't drowned out by the rest of the log."""
    lines = []
    for line in error_context.splitlines():
        if ERROR_PATTERN.search(line):
            line = re.sub(r"[=!_*#-]{3,}", " ", line).strip()[:200]  # drop banner decoration
            if len(line) >= 20:  # skip fragments like "FAILURES"
                lines.append(line)
    return list(dict.fromkeys(lines))[:max_queries] or [error_context[:500]]


def format_context(docs: list[dict]) -> str:
    return "\n\n".join(f"[{d['source']}]\n{d['text']}" for d in docs) or "(no documents retrieved)"


def user_message(context: str, log: str) -> str:
    return f"REFERENCE DOCUMENTS:\n{context}\n\nFAILED BUILD LOG:\n{log}\n\nJSON answer:"


def llm():
    """This agent's model (DIAGNOSTIC_RCA_LLM_* in .env, else the shared default)."""
    return for_agent("diagnostic_rca")


def chat_json(messages: list[dict], temperature: float = 0, retries: int = 2) -> dict:
    """Raises LLMUnavailable when the model can't be reached (callers fall back to rules)."""
    model = llm()
    for attempt in range(retries + 1):
        text = model.chat(messages, json_mode=True, temperature=temperature)
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            print(f"[warn] model returned invalid JSON (attempt {attempt + 1}/{retries + 1})")
    return {}


def rank_by_rules(error_context: str, docs: list[dict]) -> dict:
    """Fallback without an LLM: the distinct error lines, most severe first, each matched
    to the closest retrieved document when one is relevant."""
    lines = build_queries(error_context, max_queries=MAX_CAUSES)
    weight = lambda l: (2 if re.search(r"error|exception|fatal|refused|denied", l, re.I) else 1)
    lines = sorted(lines, key=weight, reverse=True)
    causes = []
    for i, line in enumerate(lines):
        doc = docs[i] if i < len(docs) else None
        causes.append({"cause": f"{line} (from the log)" + (f"; see: {doc['text'][:160]}" if doc else ""),
                       "confidence": round(max(0.2, 0.6 - 0.15 * i), 2),
                       "source": doc["source"] if doc else "none"})
    summary = (f"The build failed; the log reports: {lines[0]}" if lines
               else "The build failed but the log shows no recognisable error line.")
    return normalize({"failure_summary": summary, "ranked_causes": causes}, {d["source"] for d in docs})


def normalize(result: dict, valid_sources: set[str]) -> dict:
    """Enforce the rank_causes payload shape regardless of what the model returned."""
    causes = []
    for item in result.get("ranked_causes", []):
        if not isinstance(item, dict) or not str(item.get("cause", "")).strip():
            continue
        try:
            confidence = min(1.0, max(0.0, float(item.get("confidence", 0.0))))
        except (TypeError, ValueError):
            confidence = 0.0
        source = str(item.get("source", "none"))
        causes.append({
            "cause": str(item["cause"]).strip(),
            "confidence": round(confidence, 2),
            "source": source if source in valid_sources else "none",  # no invented citations
        })
    causes.sort(key=lambda c: c["confidence"], reverse=True)
    causes = [c for i, c in enumerate(causes) if i == 0 or c["confidence"] >= MIN_CONFIDENCE]
    if not causes:
        causes = [{"cause": "Unable to determine a cause from the log", "confidence": 0.0, "source": "none"}]
    summary = str(result.get("failure_summary", "")).strip() or "No summary produced."
    return {"failure_summary": summary, "ranked_causes": causes[:MAX_CAUSES]}


def rank_causes(error_context: str, docs: list[dict]) -> dict:
    examples = json.loads((BASE / "examples/rank_examples.json").read_text())
    example_causes = {c["cause"] for ex in examples for c in ex["output"]["ranked_causes"]}
    context = format_context(docs)

    # few-shot examples as real chat turns, so small models don't confuse them with the task
    messages = [{"role": "system", "content": (BASE / "prompts/rank_causes.txt").read_text()}]
    for ex in examples:
        messages.append({"role": "user", "content": user_message(format_context(ex.get("docs", [])), ex["log"])})
        messages.append({"role": "assistant", "content": json.dumps(ex["output"])})
    messages.append({"role": "user", "content": user_message(context, error_context)})

    sources = {d["source"] for d in docs}
    for attempt in range(3):
        result = normalize(chat_json(messages, temperature=0 if attempt == 0 else 0.4), sources)
        if not any(c["cause"] in example_causes for c in result["ranked_causes"]):
            return result
        print(f"[warn] model echoed an example answer instead of analysing the log (attempt {attempt + 1}/3)")
    return normalize({"failure_summary": "The model kept echoing example answers instead of analysing the log."}, sources)


def analyze(log_text: str) -> dict:
    """Diagnose a build log without publishing anything (the orchestrator publishes).
    Returns the rank_causes payload plus how it was produced."""
    error_context = extract_error_context(clean_log(log_text))
    docs = retrieve(build_queries(error_context))
    model = llm()
    try:
        result, source = rank_causes(error_context, docs), "llm"
    except LLMUnavailable as e:
        print(f"[warn] LLM unavailable ({e}); ranking causes with rules")
        result, source = rank_by_rules(error_context, docs), "rules"
    return {**result, "retrieved": [(d["source"], d.get("distance")) for d in docs],
            "sources": {"ranking": source}, "model": model.label if source == "llm" else None}


def run(log_path: str | None = None):
    correlation_id = str(uuid.uuid4())
    path = Path(log_path) if log_path else DEFAULT_LOG
    result = analyze(path.read_text(errors="replace"))

    top = result["ranked_causes"][0]
    action_id = publish_and_log(
        agent_name="diagnostic_rca", action_type="rank_causes",
        payload=result, correlation_id=correlation_id,
        category=f"top cause: {top['cause']}"[:200],  # stored as `decision` in reasoning_logs
        input_summary=result["failure_summary"][:300],
    )

    print(f"\nLog: {path.name}\nSummary: {result['failure_summary']}\nRetrieved docs: {result['retrieved']}")
    for i, c in enumerate(result["ranked_causes"], 1):
        print(f"  {i}. [{c['confidence']:.2f}] {c['cause']} (source: {c['source']})")
    print(f"Logged as action {action_id} (ranking by {result['sources']['ranking']})\n")


if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else None)