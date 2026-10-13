import io, json, os, re, shutil, subprocess, time, uuid
from pathlib import Path

import networkx as nx
import ollama
from dotenv import load_dotenv
from networkx.drawing.nx_pydot import read_dot

from shared.events import publish_and_log

try:  # works as `python -m agents.dependency_resolver.agent` and as a direct script
    from agents.dependency_resolver import fallback
except ImportError:  # pragma: no cover
    import fallback

load_dotenv()
MODEL = os.getenv("OLLAMA_MODEL", "llama3.2:3b")
BASE = Path(__file__).parent
VALID = ["cross_layer_split", "modeling_mistake", "sequencing_fix", "manual_seed"]
LLM_RETRIES = int(os.getenv("LLM_RETRIES", "2"))  # extra attempts after the first


def tf_binary() -> str:
    exe = shutil.which("tofu") or shutil.which("terraform")
    if not exe:
        raise RuntimeError("Neither tofu nor terraform found on PATH")
    return exe


def clean_name(n: str) -> str:
    n = re.sub(r"\x1b\[[0-9;]*m", "", n)  # strip ANSI color codes
    n = n.strip().strip('"')
    n = re.sub(r"\s*\(.*?\)\s*$", "", n)  # drop "(expand)", "(destroy)", etc.
    return n.removeprefix("[root] ")


def find_cycles_tofu(tf_dir: str) -> list[list[str]]:  # takes the folder path, returns a list of cycles (each cycle = list of resource names)
    """Terraform refuses to graph a cyclic config and reports it as an error,
    so parse the error first; fall back to SCCs on the DOT output."""

    # run "tofu graph -no-color" inside the Terraform folder and capture its output as text
    result = subprocess.run([tf_binary(), "graph", "-no-color"], cwd=tf_dir, capture_output=True, text=True)

    # merge error output and normal output into one string to search
    text = result.stderr + result.stdout

    # look for "Cycle:" and capture everything after it, up to a blank line or the end of the text
    m = re.search(r"Cycle:\s*(.+?)(?:\n\s*\n|\Z)", text, re.DOTALL)

    if m:  # a cycle error was found
        # split the captured text on commas/newlines and clean each name (remove color codes and "(expand)")
        names = [clean_name(x) for x in re.split(r",|\n", m.group(1)) if x.strip()]

        # keep only real resource names (they contain a "."), and remove duplicates while keeping order
        names = list(dict.fromkeys(n for n in names if "." in n))

        # return the cycle as a one-item list, or an empty list if no valid names remained
        return [names] if names else []

    # no cycle error, but the command still failed (e.g. you forgot to run "tofu init"): stop with a clear error
    if result.returncode != 0:
        raise RuntimeError(f"terraform graph failed (did you run terraform init?):\n{result.stderr}")

    # command succeeded: parse the graph text (DOT format) into a networkx graph
    graph = read_dot(io.StringIO(result.stdout))

    # find groups of nodes that can all reach each other (cycles); ignore single nodes; clean the names
    return [[clean_name(n) for n in c] for c in nx.strongly_connected_components(graph) if len(c) > 1]


def find_cycles(tf_dir: str) -> tuple[list[list[str]], str]:
    """Primary: tofu/terraform graph. Fallback: static parse of the .tf files.
    Returns (cycles, source) where source is "tofu" or "static"."""
    try:
        return find_cycles_tofu(tf_dir), "tofu"
    except (RuntimeError, OSError) as e:  # binary missing, not initialised, or graph failed
        print(f"[warn] tofu/terraform path failed ({e}); using static .tf analysis instead")
        return fallback.find_cycles_static(tf_dir), "static"


def extract_resource_blocks(tf_file: str, names: list[str]) -> str:
    content = Path(tf_file).read_text()
    blocks = []
    for name in names:
        short = name.split(".")[-1]
        match = re.search(rf'resource\s+"\S+"\s+"{re.escape(short)}"\s*{{.*?\n}}', content, re.DOTALL)
        if match:
            blocks.append(match.group())
    return "\n\n".join(blocks) if blocks else "Resources: " + ", ".join(names)


def chat(prompt: str) -> str:
    """Call Ollama with retries + backoff. Raises on final failure (callers fall back)."""
    last = None
    for attempt in range(LLM_RETRIES + 1):
        try:
            resp = ollama.chat(model=MODEL, messages=[{"role": "user", "content": prompt}])
            text = resp["message"]["content"].strip()
            if text:
                return text
            last = RuntimeError("empty LLM response")
        except Exception as e:  # noqa: BLE001 - connection refused, model missing, timeout...
            last = e
        if attempt < LLM_RETRIES:
            time.sleep(2 ** attempt)
    raise RuntimeError(f"LLM unavailable after {LLM_RETRIES + 1} attempts: {last}")


def classify_cycle(code: str) -> tuple[str, str]:
    """Returns (category, source) where source is "llm" or "heuristic"."""
    try:
        examples = json.loads((BASE / "examples/cycle_examples.json").read_text())
        prompt = (BASE / "prompts/classify_cycle.txt").read_text().format(
            examples=json.dumps(examples, indent=2), code=code)
        answer = chat(prompt).lower()
        for cat in VALID:
            if cat in answer:
                return cat, "llm"
        print(f"[warn] unrecognised classification {answer!r}, using heuristic classifier")
    except Exception as e:  # noqa: BLE001
        print(f"[warn] LLM classification failed ({e}); using heuristic classifier")
    return fallback.heuristic_classify(code), "heuristic"


def explain_fix(category: str, code: str, cycle: list[str]) -> tuple[str, str]:
    """Returns (explanation, source) where source is "llm" or "heuristic"."""
    try:
        prompt = (BASE / "prompts/explain_fix.txt").read_text().format(category=category, code=code)
        return chat(prompt), "llm"
    except Exception as e:  # noqa: BLE001
        print(f"[warn] LLM explanation failed ({e}); using templated explanation")
        return fallback.heuristic_explain(category, cycle), "heuristic"


def publish_safely(**kwargs) -> tuple[str, str]:
    """Try Redis + Postgres. If either is down, queue to a local outbox instead.
    Returns (action_id, sink) where sink is "redis+db" or "outbox"."""
    try:
        return publish_and_log(**kwargs), "redis+db"
    except Exception as e:  # noqa: BLE001 - redis/psycopg2 connection errors
        action_id = str(uuid.uuid4())
        path = fallback.write_outbox({"local_id": action_id, "kwargs": kwargs})
        print(f"[warn] publish failed ({e}); queued locally at {path}. "
              f"Replay with: python -m agents.dependency_resolver.agent --replay")
        return action_id, "outbox"


def run(tf_dir: str, tf_file: str):
    correlation_id = str(uuid.uuid4())
    cycles, cycle_source = find_cycles(tf_dir)
    if not cycles:
        print("No circular dependencies found.")
        return
    for cycle in cycles:
        code = extract_resource_blocks(tf_file, cycle)
        category, cat_source = classify_cycle(code)
        explanation, exp_source = explain_fix(category, code, cycle)
        action_id, sink = publish_safely(
            agent_name="dependency_resolver", action_type="propose_fix",
            payload={"cycle": cycle, "category": category, "proposed_diff": explanation,
                     "sources": {"detection": cycle_source, "classification": cat_source,
                                 "explanation": exp_source}},
            correlation_id=correlation_id, category=category, input_summary=str(cycle),
        )
        print(f"\nCycle: {cycle} (detected via {cycle_source})\n"
              f"Category: {category} ({cat_source})\n"
              f"Explanation ({exp_source}): {explanation}\n"
              f"Logged as action {action_id} via {sink}\n")


if __name__ == "__main__":
    import sys

    if "--replay" in sys.argv:
        print(f"Replayed {fallback.replay_outbox()} queued event(s).")
    else:
        # BASE is the folder where agent.py lives, so BASE / "test-infra" works from any cwd.
        run(tf_dir=str(BASE / "test-infra"), tf_file=str(BASE / "test-infra/main.tf"))