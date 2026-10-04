import io, json, os, re, shutil, subprocess, uuid
from pathlib import Path

import networkx as nx
import ollama
from dotenv import load_dotenv
from networkx.drawing.nx_pydot import read_dot

from shared.events import publish_and_log

load_dotenv()
MODEL = os.getenv("OLLAMA_MODEL", "llama3.2:3b")
BASE = Path(__file__).parent
VALID = ["cross_layer_split", "modeling_mistake", "sequencing_fix", "manual_seed"]


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


def find_cycles(tf_dir: str) -> list[list[str]]:
    """Terraform refuses to graph a cyclic config and reports it as an error,
    so parse the error first; fall back to SCCs on the DOT output."""
    result = subprocess.run([tf_binary(), "graph", "-no-color"], cwd=tf_dir, capture_output=True, text=True)
    text = result.stderr + result.stdout
    m = re.search(r"Cycle:\s*(.+?)(?:\n\s*\n|\Z)", text, re.DOTALL)
    if m:
        names = [clean_name(x) for x in re.split(r",|\n", m.group(1)) if x.strip()]
        names = list(dict.fromkeys(n for n in names if "." in n))
        return [names] if names else []
    if result.returncode != 0:
        raise RuntimeError(f"terraform graph failed (did you run terraform init?):\n{result.stderr}")
    graph = read_dot(io.StringIO(result.stdout))
    return [[clean_name(n) for n in c] for c in nx.strongly_connected_components(graph) if len(c) > 1]


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
    resp = ollama.chat(model=MODEL, messages=[{"role": "user", "content": prompt}])
    return resp["message"]["content"].strip()


def classify_cycle(code: str) -> str:
    examples = json.loads((BASE / "examples/cycle_examples.json").read_text())
    prompt = (BASE / "prompts/classify_cycle.txt").read_text().format(
        examples=json.dumps(examples, indent=2), code=code)
    answer = chat(prompt).lower()
    for cat in VALID:
        if cat in answer:
            return cat
    print(f"[warn] unrecognised classification {answer!r}, falling back to modeling_mistake")
    return "modeling_mistake"


def explain_fix(category: str, code: str) -> str:
    prompt = (BASE / "prompts/explain_fix.txt").read_text().format(category=category, code=code)
    return chat(prompt)


def run(tf_dir: str, tf_file: str):
    correlation_id = str(uuid.uuid4())
    cycles = find_cycles(tf_dir)
    if not cycles:
        print("No circular dependencies found.")
        return
    for cycle in cycles:
        code = extract_resource_blocks(tf_file, cycle)
        category = classify_cycle(code)
        explanation = explain_fix(category, code)
        action_id = publish_and_log(
            agent_name="dependency_resolver", action_type="propose_fix",
            payload={"cycle": cycle, "category": category, "proposed_diff": explanation},
            correlation_id=correlation_id, category=category, input_summary=str(cycle),
        )
        print(f"\nCycle: {cycle}\nCategory: {category}\nExplanation: {explanation}\nLogged as action {action_id}\n")


if __name__ == "__main__":
    run(tf_dir=str(BASE / "test-infra"), tf_file=str(BASE / "test-infra/main.tf"))