"""Fallbacks for the Dependency Resolver Agent.

Three things can fail at runtime, and each gets a fallback:

1. tofu/terraform missing, not initialised, or erroring  -> parse the .tf files
   statically and find cycles with networkx.
2. Ollama down / model not pulled / bad answer           -> keyword heuristic for
   the category and a templated explanation.
3. Redis or Postgres down                                -> append the event to a
   local JSONL outbox so nothing is lost, and replay it later.

Every fallback result is tagged with its source ("llm" / "heuristic",
"tofu" / "static", "redis+db" / "outbox") so the downstream consumers can
tell how much to trust it.
"""
import json
import re
import time
from pathlib import Path

import networkx as nx

OUTBOX = Path(__file__).parent / "outbox" / "pending_events.jsonl"

_RESOURCE_RE = re.compile(r'^\s*resource\s+"([^"]+)"\s+"([^"]+)"\s*\{', re.MULTILINE)
_COMMENT_RE = re.compile(r"(#|//).*?$|/\*.*?\*/", re.MULTILINE | re.DOTALL)


# --------------------------------------------------------------------------
# 1. Static cycle detection (no tofu/terraform needed)
# --------------------------------------------------------------------------
def _split_resource_blocks(content: str) -> dict[str, str]:
    """Return {"type.name": block_body} using brace counting."""
    content = _COMMENT_RE.sub("", content)
    blocks: dict[str, str] = {}
    for m in _RESOURCE_RE.finditer(content):
        depth, i = 1, m.end()
        while i < len(content) and depth:
            depth += {"{": 1, "}": -1}.get(content[i], 0)
            i += 1
        blocks[f"{m.group(1)}.{m.group(2)}"] = content[m.end():i - 1]
    return blocks


def find_cycles_static(tf_dir: str) -> list[list[str]]:
    """Build a dependency graph from references between resources in *.tf files.

    Limitations (be aware of them): only same-directory `resource` blocks are
    analysed. Modules, data sources, and dynamic references are ignored.
    """
    blocks: dict[str, str] = {}
    for tf in sorted(Path(tf_dir).glob("*.tf")):
        blocks.update(_split_resource_blocks(tf.read_text()))

    graph = nx.DiGraph()
    graph.add_nodes_from(blocks)
    for addr, body in blocks.items():
        for ref in re.findall(r"\b([A-Za-z_][\w]*\.[\w\-]+)\b", body):
            if ref in blocks:
                graph.add_edge(addr, ref)

    cycles = [sorted(c) for c in nx.strongly_connected_components(graph) if len(c) > 1]
    cycles += [[n] for n in graph.nodes if graph.has_edge(n, n)]  # self-reference
    return cycles


# --------------------------------------------------------------------------
# 2. Heuristic classification + templated explanation (no LLM needed)
# --------------------------------------------------------------------------
_KEYWORDS = {
    "cross_layer_split": ["module.", "output", "subnet", "vpc", "network", "layer"],
    "manual_seed": ["bucket", "backend", "state", "bootstrap", "seed", "s3", "dynamodb"],
    "sequencing_fix": ["provisioner", "local-exec", "remote-exec", "migration", "depends_on",
                       "null_resource", "ready", "wait"],
    "modeling_mistake": ["iam", "role", "policy", "security_group", "attach", "permission"],
}


def heuristic_classify(code: str) -> str:
    """Score each category by keyword hits; ties/zero hits -> modeling_mistake."""
    text = code.lower()
    scores = {cat: sum(text.count(k) for k in kws) for cat, kws in _KEYWORDS.items()}
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else "modeling_mistake"


_FIXES = {
    "cross_layer_split": "Split the shared value into its own layer or module so the two sides stop "
                         "reading each other's outputs; pass it one way only, via an input variable.",
    "modeling_mistake": "Remove the back-reference: one resource refers to something that already "
                        "depends on it. Attach the permission or association in a separate resource "
                        "that depends on both, instead of embedding it.",
    "sequencing_fix": "Break the loop with an explicit ordering: run the first step on its own, "
                      "then use depends_on (or a separate apply stage) for the step that needs its result.",
    "manual_seed": "Create the bootstrap resource by hand (or in a separate one-off config) once, "
                   "then reference it as existing infrastructure instead of managing it in the same graph.",
}


def heuristic_explain(category: str, cycle: list[str]) -> str:
    chain = " -> ".join(cycle + cycle[:1])
    return (f"[auto-generated fallback explanation, LLM unavailable] "
            f"The dependency chain {chain} is circular and looks like a {category} problem. "
            f"{_FIXES.get(category, _FIXES['modeling_mistake'])}")


# --------------------------------------------------------------------------
# 3. Local outbox when Redis / Postgres is unreachable
# --------------------------------------------------------------------------
def write_outbox(record: dict) -> str:
    OUTBOX.parent.mkdir(parents=True, exist_ok=True)
    record = {**record, "queued_at": time.time()}
    with OUTBOX.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")
    return str(OUTBOX)


def replay_outbox() -> int:
    """Re-publish queued events. Keeps any that still fail. Returns number sent."""
    from shared.events import publish_and_log  # imported lazily: needs redis/db

    if not OUTBOX.exists():
        return 0
    remaining, sent = [], 0
    for line in OUTBOX.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        kwargs = rec["kwargs"]
        try:
            publish_and_log(**kwargs)
            sent += 1
        except Exception as e:  # noqa: BLE001 - keep it queued, whatever the failure
            print(f"[replay] still failing: {e}")
            remaining.append(line)
    if remaining:
        OUTBOX.write_text("\n".join(remaining) + "\n", encoding="utf-8")
    else:
        OUTBOX.unlink()
    return sent