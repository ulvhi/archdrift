#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "anthropic>=0.69",
#   "rich>=13.7",
# ]
# ///
"""archdrift — architecture match gate: draw.io HLD vs Spring Boot src + Consul. Docs: README.md"""

from __future__ import annotations

import argparse, base64, html, json, os, re, sys, zlib
import urllib.error, urllib.parse, urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path

import anthropic
from rich.console import Console
from rich.padding import Padding
from rich.table import Table
from rich.text import Text


# ── acceptance criteria ──────────────────────────────────────────────────────
# The AI supplies a verdict per drawn element; the score is computed here — the model never grades itself.


class Dim(StrEnum):
    COMPONENTS = "components"  # every drawn box exists in code; every active integration is drawn
    FLOWS = "flows"            # every drawn arrow's direction + mechanism + chain order
    CONTRACTS = "contracts"    # bound identifiers (topics, Feign ids, DB names) equal the labels


class Status(StrEnum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"


WEIGHTS: dict[Dim, int] = {Dim.COMPONENTS: 40, Dim.FLOWS: 35, Dim.CONTRACTS: 25}
DRIFT_UNIT_PENALTY = 5   # per undrawn integration / pipeline, capped
DRIFT_PENALTY_CAP = 20

CRITERIA = """\
You are an architecture conformance reviewer. Compare ONE Spring Boot
microservice's implementation (Java sources, build.gradle, application*.yml,
runtime KV config) against its High-Level Design diagram. Architecture is
exactly what a diagram can draw: components (boxes — the service, datastores,
topics, caches, schedulers, external systems and clients), flows (arrows —
their direction and mechanism), and contracts (the identifiers labels bind).

The diagram is already scoped to this service's own arrows; the rest of the
subsystem is other services' responsibility. Validate it 1:1, both directions:
every drawn element must exist in the source, and every active integration in
the source must be drawn, else it is undrawn drift.

You are given the drawn components as C0, C1, … and the drawn arrows as F0,
F1, …. Return:
- component_verdicts: EXACTLY ONE entry per C-id, echoing that id. status:
  pass = the box is implemented; fail = drawn but no implementation exists
  ("implementation lacks this flow"); warn = present but imperfect.
- flow_verdicts: EXACTLY ONE entry per F-id, echoing that id, in ascending
  order. Verify the arrow's drawn direction and mechanism (REST/Feign call,
  Kafka produce/consume, datastore read/write, cache access).
- code_step_order: if <hld_steps> is non-empty, return the given step ids
  (S0, S1, …) reordered into the sequence the CODE actually executes them
  (e.g. Spring Batch writes job metadata at job start, a reader runs before a
  writer). Report the true code order even if it equals the drawn order — the
  tool compares the two deterministically and fails any mismatch. Empty when
  there are no numbered steps.
- contract_checks: one row per bound identifier you can verify (Kafka topic
  name, Feign service id, DB schema/collection) — expected = the drawn label,
  actual = the resolved binding. A renamed identifier is fail.
Never omit a C-id or F-id; if unsure, use warn, never silence.

"actual" is COMPONENT LEVEL: the implementing class plus the RESOLVED runtime
value (URL, topic, namespace) — "CardPaymentClient -> http://ms-card-payment".
FORBIDDEN: ${...} placeholders, config key paths, method calls or signatures,
code expressions. The reader is an engineer reviewing architecture, not a
coder debugging. Keep expected <= 10 words, actual <= 15 words.

Evidence: code declarations (annotations, producers/consumers, repositories,
clients) prove an integration; values resolved outside the artifacts (${...}
placeholders, vault imports) prove nothing — never fail a component merely
because a URL is an unresolved placeholder. Config matters only where it binds
an architectural identifier — topic names, Feign ids/urls, DB names; client
tuning (timeouts, batch sizes, retries, consumer group ids, broker properties)
is invisible to this gate. Only invoked integrations are architecture —
declared-but-never-used code goes to "dormant", never to a verdict. Match
components semantically ("Debt MS" may be DebtClient); match identifiers
literally. A bracketed/parenthesized qualifier in a label ("ms-card
[birbank-backend]") is judged deterministically by the tool — do NOT mark a
component warn or fail on account of a namespace, and do not emit contract
rows about namespaces; just report the class and the resolved URL as actual.

A diagram draws COMPONENTS and the edges between them, never internal classes.
Every source/target you name — in flow_verdicts, undrawn_flows and extra — must
be an architecture component: this service (by name), a topic/queue, a
datastore, a cache, or an external system/client. NEVER an implementation class
(consumer, producer, listener, scheduler, strategy, processor, handler,
repository, service bean). Many listener/producer classes on the same topic are
ONE edge, not many — collapse them.

When several undrawn integrations form ONE connected pipeline (this service
consumes a topic then calls a downstream system), report it once in
"undrawn_flows" as ordered component hops (source, mechanism, target) — e.g.
"payment.x -> consume -> ms-this-service -> REST -> ms-other", not the class
chain. Standalone undrawn integrations go in "extra". Show a missing flow end
to end, at component level.

<undrawn_bindings> lists names the runtime provably binds that NO drawn arrow
covers — computed deterministically; never contradict it. For EACH listed
name, trace its producers, consumers, schedulers and downstream calls, and
report that pipeline in "undrawn_flows" (or "dormant" if no code uses it)."""

VERDICT_ITEM = {
    "type": "object",
    "properties": {
        "id": {"type": "string", "description": "Echo the given C-id / F-id, e.g. C0."},
        "status": {"type": "string", "enum": [s.value for s in Status]},
        "actual": {"type": "string", "description": "Class + resolved value. <= 15 words."},
    },
    "required": ["id", "status", "actual"],
    "additionalProperties": False,
}
AUDIT_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "description": "Two-sentence architect's verdict."},
        "component_verdicts": {"type": "array", "items": VERDICT_ITEM},
        "flow_verdicts": {"type": "array", "items": VERDICT_ITEM},
        "contract_checks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "status": {"type": "string", "enum": [s.value for s in Status]},
                    "expected": {"type": "string", "description": "Drawn label. <= 10 words."},
                    "actual": {"type": "string", "description": "Resolved binding. <= 15 words."},
                },
                "required": ["status", "expected", "actual"],
                "additionalProperties": False,
            },
        },
        "code_step_order": {"type": "array", "items": {"type": "string"},
                            "description": "The step ids (S0, S1, …) from <hld_steps> reordered into "
                                           "the sequence the CODE actually executes them."},
        "extra": {"type": "array", "items": {"type": "string"},
                  "description": "Standalone active integrations in code the HLD does not draw."},
        "dormant": {"type": "array", "items": {"type": "string"},
                    "description": "Declared but never-invoked components. Informational only."},
        "undrawn_flows": {
            "type": "array",
            "description": "Connected pipelines fully absent from the HLD, end to end.",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Flow name. <= 6 words."},
                    "hops": {"type": "array", "items": {
                        "type": "object",
                        "properties": {
                            "source": {"type": "string"},
                            "mechanism": {"type": "string",
                                          "description": "schedule / produce / consume / REST / read / write"},
                            "target": {"type": "string"},
                        },
                        "required": ["source", "mechanism", "target"],
                        "additionalProperties": False,
                    }},
                },
                "required": ["title", "hops"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["summary", "component_verdicts", "flow_verdicts", "contract_checks",
                 "code_step_order", "extra", "dormant", "undrawn_flows"],
    "additionalProperties": False,
}


# ── data model ───────────────────────────────────────────────────────────────


STEP_RE = re.compile(r"^(\d+(?:\.\d+)*)\s+")
type Hop = tuple[str, str, str]      # (source, mechanism, target)


@dataclass(slots=True, frozen=True)
class Diagram:
    components: tuple[str, ...]
    edges: tuple[tuple[str, str, str], ...]       # (source, label, target)
    steps: tuple[tuple[str, str, str, str], ...] = ()  # (text, source, target, suspect) per labeled arrow
    loose_ends: tuple[tuple[str, str], ...] = ()  # (glued endpoint, component under the free head)


@dataclass(slots=True, frozen=True)
class Check:
    dimension: Dim
    status: Status
    expected: str   # the HLD element
    actual: str     # what the source implements


@dataclass(slots=True, frozen=True)
class Audit:
    summary: str
    extra: tuple[str, ...]                                    # standalone active drift — scored
    dormant: tuple[str, ...]                                  # dead code — informational, never scored
    undrawn_flows: tuple[tuple[str, tuple[Hop, ...]], ...] = ()  # (title, hops)
    checks: tuple[Check, ...] = ()
    code_step_order: tuple[str, ...] = ()                    # step ids in the order the CODE runs them


# ── collectors ───────────────────────────────────────────────────────────────


def parse_diagram(path: Path) -> Diagram:
    # TODO: parse .drawio (plain or deflate-compressed pages) into components + labeled edges.

    def _inflate(payload: str) -> str:
        # TODO: draw.io compresses page XML as base64(raw-deflate(url-encoded)).
        raw = zlib.decompress(base64.b64decode(payload), wbits=-15)
        return urllib.parse.unquote(raw.decode())

    def _label(node: ET.Element, cell: ET.Element) -> str:
        # TODO: resolve cell name — expand C4 %attr% placeholders, strip HTML, drop paste artifacts.
        raw = node.get("label") or cell.get("value") or ""
        raw = re.sub(r"%(\w+)%", lambda m: node.get(m.group(1), ""), raw)
        text = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", raw))).strip()
        return "" if len(text) > 80 or text.startswith("%3C") else text

    def _models(page: ET.Element) -> list[ET.Element]:
        # TODO: normalize a <diagram> page (child XML or compressed text) to <mxGraphModel>.
        if found := page.findall(".//mxGraphModel"):
            return found
        if page.text and page.text.strip():
            return [ET.fromstring(_inflate(page.text))]
        return []

    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError) as err:
        sys.exit(f"cannot read diagram '{path}': {err}")
    pages = [root] if root.tag == "mxGraphModel" else [
        model for page in root.iter("diagram") for model in _models(page)
    ]

    vertices: list[tuple[str, str, str]] = []    # (id, parent id, label text)
    raw_edges: dict[str, list[str]] = {}         # edge id -> [source_id, label, target_id]
    boxes: dict[str, tuple[float, ...]] = {}     # vertex id -> bbox (x, y, w, h)
    loose: dict[str, tuple[float, float]] = {}   # edge id -> free endpoint coordinates
    for model in pages:
        for node in model.iter():
            cell = node if node.tag == "mxCell" else node.find("mxCell")
            if cell is None:
                continue
            cid = node.get("id") or cell.get("id") or ""
            text = _label(node, cell)
            style = cell.get("style") or ""
            geo = cell.find("mxGeometry")
            if cell.get("edge") == "1":
                raw_edges[cid] = [cell.get("source") or "", text, cell.get("target") or ""]
                for point in (geo.iter("mxPoint") if geo is not None else ()):
                    if point.get("as") in ("sourcePoint", "targetPoint"):
                        loose[cid] = (float(point.get("x", 0)), float(point.get("y", 0)))
            elif cell.get("vertex") == "1" and len(text) > 1 and not style.startswith("text;"):
                vertices.append((cid, cell.get("parent") or "", text))
                if geo is not None and geo.get("x") is not None:
                    boxes[cid] = (float(geo.get("x")), float(geo.get("y", 0)),
                                  float(geo.get("width", 0)), float(geo.get("height", 0)))

    labels: dict[str, str] = {}                  # vertex id -> component label
    for cid, parent, text in vertices:
        if parent in raw_edges:                  # a label riding an arrow belongs to that edge
            raw_edges[parent][1] = f"{raw_edges[parent][1]} {text}".strip()
        elif not STEP_RE.match(text):            # prose annotations ≠ components
            labels[cid] = text

    def _suspect(eid: str) -> str:
        # TODO: name the component sitting under an unglued arrow head (bbox hit-test).
        if (pt := loose.get(eid)) is None:
            return ""
        px, py = pt
        return next((labels[vid] for vid, (x, y, w, h) in boxes.items()
                     if vid in labels
                     and x - 15 <= px <= x + w + 15 and y - 15 <= py <= y + h + 15), "")

    edges, steps, loose_ends = [], [], []
    for eid, (s, label, t) in raw_edges.items():
        source, target = labels.get(s, ""), labels.get(t, "")
        if source and target:
            edges.append((source, label, target))
        else:                                        # unglued arrow: keep who holds it + what its free head sits on
            loose_ends.append((source or target, _suspect(eid)))
        if STEP_RE.match(label):                     # steps survive even on dangling arrows
            steps.append((label, source, target,
                          "" if source and target else _suspect(eid)))

    return Diagram(components=tuple(dict.fromkeys(labels.values())),
                   edges=tuple(edges), steps=tuple(steps), loose_ends=tuple(loose_ends))


def scope_to_service(diagram: Diagram, service: str) -> Diagram:
    # TODO: cut the HLD to this service's ego network; steps = labels riding its arrows, ascending.
    edges = tuple(e for e in diagram.edges if service in (e[0], e[2]))
    keep = {service, *(n for s, _, t in edges for n in (s, t))}
    steps = tuple(sorted(
        (step for step in diagram.steps if service in (step[1], step[2])),
        key=lambda step: tuple(map(int, STEP_RE.match(step[0]).group(1).split("."))),
    ))
    return Diagram(components=tuple(c for c in diagram.components if c in keep),
                   edges=edges, steps=steps,
                   loose_ends=tuple(le for le in diagram.loose_ends if le[0] == service))


def harvest_codebase(root: Path) -> dict[str, str]:
    # TODO: collect architecture artifacts — Java sources (minus DTOs/entities), gradle, application*.yml.
    skip_dirs = {"dto", "dtos", "entity", "entities", "domain", "model", "models", "build", "bin"}
    skip_suffixes = ("Dto.java", "Entity.java", "Request.java", "Response.java")

    def _architectural(p: Path) -> bool:
        # TODO: keep wiring/behavior classes, drop pure data shapes.
        return not (
            skip_dirs & {part.lower() for part in p.parts}
            or p.name.endswith(skip_suffixes)
        )

    java = sorted(p for p in root.rglob("src/main/java/**/*.java") if _architectural(p))
    config = [root / "build.gradle", *sorted(root.rglob("src/main/resources/**/application*.yml"))]
    return {
        str(p.relative_to(root)): p.read_text(encoding="utf-8", errors="replace")
        for p in (*java, *config)
        if p.is_file()
    }


def redaction_map(spec: str) -> dict[str, str]:
    # TODO: organisation identifiers (company name in package prefixes, internal host domains) are not
    # architecture — map them to neutral tokens before anything leaves, and map them back on the way in.
    return {
        term: token or f"org{n + 1}"
        for n, (term, _, token) in enumerate(
            item.partition("=") for item in re.split(r"[,\s]+", spec) if item.strip())
    }


def apply_map(text: str, mapping: dict[str, str], *, reverse: bool = False) -> str:
    # TODO: deterministic substitution both ways — the model sees tokens, the engineer reads real names.
    for real, token in mapping.items():
        source, target = (token, real) if reverse else (real, token)
        text = re.sub(re.escape(source), target, text, flags=re.IGNORECASE)
    return text


def fetch_consul(url: str) -> str | None:
    # TODO: pull deployed runtime config from live Consul KV; unreachable -> skip config checks, not the run.
    request = urllib.request.Request(url)
    if token := os.environ.get("CONSUL_HTTP_TOKEN"):
        request.add_header("X-Consul-Token", token)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.read().decode()
    except (urllib.error.URLError, TimeoutError, OSError):
        return None


# ── audit ────────────────────────────────────────────────────────────────────


def build_checks(verdict: dict, diagram: Diagram) -> tuple[tuple[Check, ...], list[str]]:
    # TODO: reconcile model verdicts against the KNOWN scoped elements — one row per drawn
    # component and arrow guaranteed; anything the model skipped is backfilled, never dropped.
    def _index(items: list[dict], prefix: str) -> dict[int, dict]:
        out: dict[int, dict] = {}
        for item in items:
            if m := re.search(rf"{prefix}(\d+)", str(item.get("id", ""))):
                out.setdefault(int(m.group(1)), item)
        return out

    comp_v = _index(verdict.get("component_verdicts", []), "C")
    flow_v = _index(verdict.get("flow_verdicts", []), "F")
    checks: list[Check] = []
    unassessed: list[str] = []

    for i, comp in enumerate(diagram.components):
        if v := comp_v.get(i):
            checks.append(Check(Dim.COMPONENTS, Status(v["status"]), comp, v["actual"]))
        else:
            checks.append(Check(Dim.COMPONENTS, Status.WARN, comp, "not assessed — rerun or use a stronger model"))
            unassessed.append(f"C{i}")
    for i, (s, label, t) in enumerate(diagram.edges):
        # keep the edge label in the row so two edges between the same nodes stay distinct
        expected = f"{s} → {t}" + (f"  [{label}]" if label and not STEP_RE.match(label) else "")
        if v := flow_v.get(i):
            checks.append(Check(Dim.FLOWS, Status(v["status"]), expected, v["actual"]))
        else:
            checks.append(Check(Dim.FLOWS, Status.WARN, expected, "not assessed — rerun or use a stronger model"))
            unassessed.append(f"F{i}")
    for c in verdict.get("contract_checks", []):
        checks.append(Check(Dim.CONTRACTS, Status(c["status"]), c["expected"], c["actual"]))
    return tuple(checks), unassessed


def run_audit(diagram: Diagram, sources: dict[str, str], consul: str | None, service: str,
              model: str, undrawn_hints: tuple[str, ...] = (),
              redact: dict[str, str] | None = None) -> Audit:
    # TODO: stream the evidence bundle to the model; reconcile its verdicts against the known scope.

    def _bundle(extra_note: str = "") -> str:
        # TODO: assemble one citable evidence document — elements id-tagged, runtime truth last.
        blocks = [
            f"<service>{service}</service>",
            "<hld_components>\n" + "\n".join(f"C{i}: {c}" for i, c in enumerate(diagram.components))
            + "\n</hld_components>",
            "<hld_edges>\n" + "\n".join(
                f"F{i}: {s} --[{label or 'unlabeled'}]--> {t}"
                for i, (s, label, t) in enumerate(diagram.edges)
            ) + "\n</hld_edges>",
            "<hld_steps note='drawn execution order, ascending. Return code_step_order = these ids in "
            "the order the CODE runs them.'>\n" + "\n".join(
                f"S{i}: {text}  (on arrow: {source or '?'} -> {target or suspect or '?'})"
                for i, (text, source, target, suspect) in enumerate(diagram.steps)
            ) + "\n</hld_steps>",
            *(f'<file path="{path}">\n{body}\n</file>' for path, body in sources.items()),
        ]
        if consul:
            blocks.append(f"<consul_kv note='overrides application.yml at runtime'>\n{consul}\n</consul_kv>")
        if undrawn_hints:
            blocks.append(
                "<undrawn_bindings note='deterministic: bound in runtime config, NOT drawn on this service arrows'>\n"
                + "\n".join(f"- {n}" for n in undrawn_hints) + "\n</undrawn_bindings>")
        if extra_note:
            blocks.append(extra_note)
        return apply_map("\n\n".join(blocks), redact) if redact else "\n\n".join(blocks)

    client = anthropic.Anthropic()

    def _ask(content: str) -> dict:
        # TODO: one structured call; surface API/parse failures as clean messages, not tracebacks.
        try:
            with client.messages.stream(
                model=model, max_tokens=32_000, system=CRITERIA,
                output_config={"format": {"type": "json_schema", "schema": AUDIT_SCHEMA}},
                messages=[{"role": "user", "content": content}],
            ) as stream:
                message = stream.get_final_message()
        except anthropic.APIError as err:
            sys.exit(f"audit failed: {type(err).__name__} — {err}")
        if message.stop_reason == "max_tokens":
            sys.exit("audit response truncated — diagram/codebase too large for one pass")
        if message.stop_reason != "end_turn":
            sys.exit(f"audit aborted: model stopped with '{message.stop_reason}'")
        try:
            body = next(b.text for b in message.content if b.type == "text")
        except StopIteration:
            sys.exit("audit returned no verdict")
        if redact:                       # restore real names locally — the vendor never saw them
            body = apply_map(body, redact, reverse=True)
        try:
            return json.loads(body)
        except json.JSONDecodeError as err:
            sys.exit(f"audit returned no parseable verdict: {err}")

    verdict = _ask(_bundle())
    checks, unassessed = build_checks(verdict, diagram)
    if unassessed:                               # one corrective retry: cheap models drop rows under load
        verdict = _ask(_bundle(
            "<corrective note='the previous response omitted a verdict for: "
            + ", ".join(unassessed)
            + "'>\nReturn component_verdicts AND flow_verdicts with EXACTLY ONE entry per "
              "C-id and F-id shown above. Do not skip any.\n</corrective>"))
        checks, _ = build_checks(verdict, diagram)

    return Audit(
        summary=verdict["summary"],
        extra=tuple(verdict["extra"]),
        dormant=tuple(verdict["dormant"]),
        undrawn_flows=tuple(
            (flow["title"], tuple((h["source"], h["mechanism"], h["target"]) for h in flow["hops"]))
            for flow in verdict["undrawn_flows"]
        ),
        checks=checks,
        code_step_order=tuple(verdict.get("code_step_order", ())),
    )


# ── deterministic cross-checks ─────────────────────────────────────────────────


PHASE = {"fetch": 1, "read": 1, "get": 1, "load": 1, "query": 1, "consume": 1, "receive": 1, "poll": 1,
         "process": 2, "filter": 2, "transform": 2, "compute": 2, "validate": 2, "enrich": 2,
         "send": 3, "produce": 3, "publish": 3, "emit": 3, "write": 3, "save": 3, "update": 3, "push": 3,
         "notify": 3, "dispatch": 3}


def _phase(label: str) -> int | None:
    # TODO: map a step's action verb to a pipeline phase — setup(0) < read(1) < process(2) < output(3).
    low = label.lower()
    if any(k in low for k in ("metadata", "checkpoint", "shedlock", "init")):
        return 0                                        # batch/setup writes happen at job start
    return next((PHASE[w] for w in re.findall(r"[a-z]+", low) if w in PHASE), None)


def cross_check_order(diagram: Diagram, code_order: tuple[str, ...]) -> tuple[Check, ...]:
    # TODO: two independent order guards. (a) DETERMINISTIC — the drawn numbering must not violate the
    # natural pipeline phase order (a produce numbered before a later read/setup is wrong), needs no model.
    # (b) The model reports the code's real order and the tool compares it to the drawn numbering.
    n = len(diagram.steps)
    if n < 2:
        return ()
    checks: list[Check] = []

    phases = [_phase(text) for text, *_ in diagram.steps]
    if all(p is not None for p in phases) and phases != sorted(phases):
        drawn = " → ".join(text for text, *_ in diagram.steps)
        expected = " → ".join(text for _, text in sorted(zip(phases, (s[0] for s in diagram.steps))))
        checks.append(Check(Dim.FLOWS, Status.FAIL, "flow steps out of order",
                            f"drawn {drawn}; phases imply {expected} (setup→read→process→output)"))

    code, seen = [], set()
    for entry in code_order:
        if (m := re.search(r"S(\d+)", str(entry))) and (i := int(m.group(1))) < n and i not in seen:
            seen.add(i)
            code.append(i)
    if len(code) == n and code != list(range(n)):
        checks.append(Check(Dim.FLOWS, Status.FAIL, "flow steps out of order",
                            f"code runs {' → '.join(diagram.steps[i][0] for i in code)}; "
                            f"HLD numbers them {' → '.join(s[0] for s in diagram.steps)}"))
    return tuple(checks)


TOPIC_RE = re.compile(r"[a-z][a-z0-9-]*(?:\.[a-z0-9-]+){2,}")


def _config_text(sources: dict[str, str], consul: str | None) -> str:
    # TODO: one config document — local yml plus live Consul KV (runtime truth).
    return "\n".join((
        *(body for path, body in sources.items() if path.endswith((".yml", ".yaml"))),
        consul or "",
    ))


def bound_topics(text: str) -> dict[str, set[str]]:
    # TODO: indentation-aware YAML walk — topic VALUES under topic keys, each tagged with the
    # direction its key context implies (listener/consumer -> consume, producer -> produce).
    stack: list[tuple[int, str]] = []
    found: dict[str, set[str]] = {}
    for line in text.splitlines():
        if not (m := re.match(r"^(\s*)([A-Za-z0-9_.\-]+):\s*(.*?)\s*$", line)):
            continue
        indent, key, value = len(m[1]), m[2].lower(), m[3].strip("'\"")
        while stack and stack[-1][0] >= indent:
            stack.pop()
        stack.append((indent, key))
        keys = [k for _, k in stack]
        topical = "topic" in key or (key == "name" and any("topic" in k for k in keys[:-1]))
        if (topical and value and "${" not in value and TOPIC_RE.fullmatch(value)
                and not value.lower().endswith((".retry", ".dlt"))):
            joined = " ".join(keys)
            direction = {"consume"} if ("listener" in joined or "consumer" in joined) else set()
            direction |= {"produce"} if "producer" in joined else set()
            found.setdefault(value, set()).update(direction)
    return found


def scheduler_names(sources: dict[str, str]) -> set[str]:
    # TODO: classes that drive periodic work (@Scheduled / *Scheduler) — the trigger end of a produced pipeline.
    return {
        Path(path).stem for path, body in sources.items()
        if path.endswith(".java") and ("@Scheduled" in body or path.endswith("Scheduler.java"))
    }


def outbound_urls(text: str) -> dict[str, str]:
    # TODO: outbound HTTP integrations (Feign/REST) from config `<service>: url: http…`, including
    # library clients (Atlas) with no @FeignClient here. Only http(s) values — a JDBC datasource,
    # a ${...} placeholder, or an auth endpoint is NOT a REST call and is excluded.
    infra = {"datasource", "mongodb", "redis", "r2dbc", "flyway", "liquibase"}
    stack: list[tuple[int, str]] = []
    urls: dict[str, str] = {}
    for line in text.splitlines():
        if not (m := re.match(r"^(\s*)([\w.-]+):\s*(.*?)\s*$", line)):
            continue
        indent, key, value = len(m[1]), m[2].lower(), m[3].strip("'\"")
        while stack and stack[-1][0] >= indent:
            stack.pop()
        stack.append((indent, key))
        parent = stack[-2][1] if len(stack) >= 2 else ""
        path = " ".join(k for _, k in stack)
        if (key == "url" and value.startswith(("http://", "https://")) and parent not in infra
                and not any(a in path for a in ("token", "jwk", "issuer", "logout", "keycloak", "oauth"))):
            urls[parent] = value
    return urls


def client_method(token: str, sources: dict[str, str]) -> str:
    # TODO: the business step is the invoked client method — e.g. AtlasClient.getNationalExchangeRate();
    # name it from the import (class) + the call site (method), since library clients have no local paths.
    tok = token.replace("-", "").lower()
    call_re = re.compile(rf"\b\w*{re.escape(tok)}\w*[Cc]lient\.(\w+)\s*\(")
    candidates, methods = set(), set()
    for path, body in sources.items():
        if not path.endswith(".java"):
            continue
        for m in re.finditer(r"import\s+([\w.]+);", body):
            seg = m.group(1).split(".")[-1]
            if tok in seg.lower() and seg[:1].isupper():
                candidates.add(seg)
        methods.update(m.group(1) for m in call_re.finditer(body))
    if not methods:
        return ""
    clients = [c for c in candidates if c.endswith(("Client", "Api", "Gateway", "Feign"))]
    cls = min(clients or candidates, key=len, default=token)   # prefer the *Client type over config/model classes
    return f"{cls}.{sorted(methods)[0]}()" + (f" (+{len(methods) - 1})" if len(methods) > 1 else "")


def cross_check_clients(audit: Audit, urls: dict[str, str], sources: dict[str, str],
                        service: str) -> Audit:
    # TODO: deterministic guarantee for outbound integrations (the model drops library clients like
    # Atlas at random) — any config url the model never accounted for is undrawn drift, named by the
    # invoked client method (the business step). accounted-for = a real coverage signal (check row,
    # extra, dormant, undrawn flow), NOT the loose summary prose.
    seen = " ".join((*audit.extra, *audit.dormant,
                     *(f"{c.expected} {c.actual}" for c in audit.checks),
                     *(f"{s} {t}" for _, hops in audit.undrawn_flows for s, _, t in hops))).lower()
    flows, checks = list(audit.undrawn_flows), list(audit.checks)
    for token, url in urls.items():
        endpoint = re.sub(r"^https?://", "", url).rstrip("/")   # host + base path, e.g. pre.atlas…az/api
        host = endpoint.split("/")[0]
        if token in seen or host in seen:            # model mapped it to a drawn component / reported it
            continue
        step = client_method(token, sources) or token           # business step: the invoked lib method
        flows.append((f"{token} (undrawn)", ((service, "REST", f"{step}  ·  {endpoint}"),)))
        checks.append(Check(Dim.FLOWS, Status.FAIL, "(not in HLD)",
                            f"{service} calls {step} at {endpoint} — outbound call not drawn"))
    return replace(audit, undrawn_flows=tuple(flows), checks=tuple(checks))


def topic_diff(diagram: Diagram, sources: dict[str, str], consul: str | None,
               service: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    # TODO: node-level topic diff — bound topic names vs HLD topic nodes on this service's arrows.
    bound = set(bound_topics(_config_text(sources, consul)))
    drawn = {
        node
        for source, _, target in diagram.edges if service in (source, target)
        for node in (source, target)
        if node != service and TOPIC_RE.fullmatch(node)
    }
    return tuple(sorted(drawn - bound)), tuple(sorted(bound - drawn))


def cross_check_directions(diagram: Diagram, sources: dict[str, str], consul: str | None,
                           service: str) -> tuple[Check, ...]:
    # TODO: direction-level guard — a topic drawn ONE way (only produce, or only consume) while the
    # code does the other is undrawn drift a node-level check misses (self-loops, produce+consume).
    dirs = bound_topics(_config_text(sources, consul))
    produced = {t for s, _, t in diagram.edges if s == service}   # service -> topic
    consumed = {s for s, _, t in diagram.edges if t == service}   # topic -> service
    return tuple(
        Check(Dim.FLOWS, Status.FAIL, f"{topic} — {arrow} arrow",
              f"code {verb} it, but no {arrow} arrow is drawn")
        for topic, d in dirs.items() if topic in produced | consumed  # fully-undrawn handled by topic_diff
        for verb, arrow, drawn in (("consumes", "consume", consumed), ("produces", "produce", produced))
        if verb[:-1] in d and topic not in drawn
    )


def enforce_hints(audit: Audit, hints: tuple[str, ...], directions: dict[str, set[str]],
                  schedulers: set[str], service: str) -> Audit:
    # TODO: undrawn topic pipelines are built DETERMINISTICALLY, not from the model's inconsistent prose —
    # canonical lifecycle [scheduler ▸] produce ▸ topic ▸ consume ▸ service, every run. This also stops the
    # model conflating the undrawn topic-consume with already-drawn downstream REST edges.
    hintset = set(hints)
    flows = [(title, hops) for title, hops in audit.undrawn_flows          # keep only non-topic drift
             if not any(topic in (s, t) for s, _, t in hops for topic in hintset)]
    checks = list(audit.checks)
    for topic in hints:
        d = directions.get(topic) or set()
        seg = topic.rsplit(".", 1)[-1].replace("-", "")                    # last segment: reminder / execution
        trigger = next((s for s in schedulers if len(seg) > 3 and seg in s.lower()), None)
        hops: list[Hop] = []
        if "produce" in d:
            if trigger:
                hops.append((trigger, "schedule", service))
            hops.append((service, "produce", topic))
        if "consume" in d:
            hops.append((topic, "consume", service))
        flows.append((topic, tuple(hops) or ((topic, "bound in", service),)))
        verb = " & ".join(v for v, k in (("produces", "produce"), ("consumes", "consume")) if k in d)
        checks.append(Check(Dim.FLOWS, Status.FAIL, "(not in HLD)",
                            f"{service} {verb or 'binds'} {topic} — not drawn in HLD"))
    return replace(audit, undrawn_flows=tuple(flows), checks=tuple(checks))


def cross_check_names(not_bound: tuple[str, ...], undrawn: tuple[str, ...]) -> tuple[Check, ...]:
    # TODO: render the deterministic topic diff as aggregated contract rows.
    return tuple(
        Check(Dim.CONTRACTS, Status.FAIL, expected, f"{actual}: {', '.join(names)}")
        for names, expected, actual in (
            (not_bound, "every drawn topic bound in config", "not bound anywhere"),
            (undrawn, "only drawn topics bound in config", "config binds undrawn"),
        )
        if names
    )


def cross_check_steps(diagram: Diagram) -> tuple[Check, ...]:
    # TODO: deterministic chain guards — duplicate step numbers fail, unglued step arrows warn.
    counts = Counter(m.group(1) for text, *_ in diagram.steps if (m := STEP_RE.match(text)))
    duplicates = tuple(
        Check(Dim.FLOWS, Status.FAIL, "unique step numbers in chain",
              f"step number '{num}' used {n}× — sequence ambiguous")
        for num, n in counts.items() if n > 1
    )
    unattached = tuple(
        Check(Dim.FLOWS, Status.WARN, f"'{text}' arrow glued to a component",
              f"arrow head sits on '{suspect}' but is not glued — drag its endpoint onto it"
              if suspect else "arrow attached to nothing — glue it in draw.io")
        for text, source, target, suspect in diagram.steps if not (source and target)
    )
    return (*duplicates, *unattached)


QUALIFIER_RE = re.compile(r"[\[(]([a-z0-9][a-z0-9.-]+)[\])]")  # namespace/target token, not descriptive prose
ABSENT_RE = re.compile(r"not implement|no impl|missing|absent|lacks|not found|no client|no code",
                       re.IGNORECASE)
HOST_RE = re.compile(r"[a-z0-9-]+\.[a-z0-9-]+")               # a resolved host/namespace to judge against
UNRESOLVED_RE = re.compile(r"\$\{|placeholder|unresolved|vault", re.IGNORECASE)


def enforce_qualifiers(audit: Audit) -> Audit:
    # TODO: a label's namespace qualifier ("[birbank-backend]") is a deterministic contract — the
    # model must NOT judge it (it flip-flops). For an existing component with a RESOLVED value we own
    # the verdict: namespace present -> pass, absent -> warn. A placeholder/vault value can't be judged
    # (our rule: it proves nothing, never a mismatch) -> pass; genuinely missing -> leave as-is.
    def _fix(c: Check) -> Check:
        if c.dimension is not Dim.COMPONENTS:
            return c
        quals = QUALIFIER_RE.findall(c.expected.lower())
        actual = c.actual.lower()
        if not quals or ABSENT_RE.search(actual):
            return c
        if UNRESOLVED_RE.search(c.actual) or not HOST_RE.search(actual):  # nothing resolved to judge
            return replace(c, status=Status.PASS)
        matched = all(q in actual for q in quals)
        return replace(c, status=Status.PASS if matched else Status.WARN)

    return replace(audit, checks=tuple(map(_fix, audit.checks)))


def neutralize_placeholders(audit: Audit) -> Audit:
    # TODO: enforce the core rule the model keeps breaking — a ${...}/vault/placeholder value proves
    # nothing and is never a mismatch; flip any warn that only cites an unresolved value back to pass.
    return replace(audit, checks=tuple(
        replace(c, status=Status.PASS)
        if c.status is Status.WARN and UNRESOLVED_RE.search(c.actual) else c
        for c in audit.checks
    ))


def suppress_dormant(audit: Audit) -> Audit:
    # TODO: deterministically drop non-pass checks naming components the audit marked dormant.
    def _idents(text: str) -> set[str]:
        # TODO: comparable identifier tokens, minus filler vocabulary.
        return {w.lower() for w in re.findall(r"[A-Za-z][\w-]{3,}", text)} - {
            "feign", "client", "kafka", "topic", "never", "invoked",
            "unused", "dormant", "bean", "declared", "active",
        }

    dormant = set().union(*map(_idents, audit.dormant)) if audit.dormant else set()
    return replace(
        audit,
        extra=tuple(e for e in audit.extra if not dormant & _idents(e)),
        checks=tuple(
            c for c in audit.checks
            if c.status is Status.PASS or not dormant & _idents(f"{c.expected} {c.actual}")
        ),
    )


def score(audit: Audit) -> tuple[int, dict[Dim, int | None], int]:
    # TODO: deterministic grade — weighted pass-rate (warn = half credit) minus capped drift penalty;
    # a dimension with no checks is unjudged (None), its weight redistributes — never a silent 0%.
    credit = {Status.PASS: 1.0, Status.WARN: 0.5, Status.FAIL: 0.0}
    per_dim: dict[Dim, int | None] = {}
    for dim in Dim:
        checks = [c for c in audit.checks if c.dimension is dim]
        per_dim[dim] = round(
            100 * sum(credit[c.status] for c in checks) / len(checks)
        ) if checks else None

    judged = {d: w for d, w in WEIGHTS.items() if per_dim[d] is not None}
    weighted = (sum(per_dim[d] * w for d, w in judged.items()) / sum(judged.values())
                if judged else 0)
    drift_units = len(audit.extra) + len(audit.undrawn_flows)
    penalty = min(drift_units * DRIFT_UNIT_PENALTY, DRIFT_PENALTY_CAP)
    return max(round(weighted) - penalty, 0), per_dim, penalty


# ── terminal report ──────────────────────────────────────────────────────────


GLYPH = {Status.PASS: ("●", "green"), Status.WARN: ("▲", "yellow"), Status.FAIL: ("✕", "red")}


def _polish(text: str) -> str:
    # TODO: high-level readability — strip method-call chains, ${...} placeholders and config key paths; keep classes, URLs, names.
    text = re.sub(r"\.\w+\((?:[^()]|\([^()]*\))*\)", "", text)   # .makeAction(args...) -> ''
    text = re.sub(r"\$\{[^}]*\}=?", "", text)                    # ${api.card.url}= -> ''
    text = re.sub(r"\b[\w-]+(?:\.[\w-]+){2,}=", "", text)        # application...topic.name= -> ''
    return re.sub(r"\s+", " ", text).strip(" ,;=")


def render(console: Console, service: str, diagram: Diagram, audit: Audit, total: int,
           per_dim: dict[Dim, int | None], penalty: int, threshold: int,
           profile: str, notices: list[str]) -> None:
    passed = total >= threshold
    accent = "green" if passed else "red"
    grade = ("CLEAN" if total >= 90 else "PASSING" if passed
             else "DRIFTING" if total >= 50 else "BROKEN")

    def _bar(value: int | None, width: int = 22) -> Text:
        # TODO: color-banded block bar; unjudged dimension reads as hollow.
        if value is None:
            return Text("░" * width + "  n/a", style="dim")
        filled = round(value / 100 * width)
        color = "green" if value >= 80 else "yellow" if value >= 50 else "red"
        return Text("█" * filled + "░" * (width - filled) + f"  {value}%", style=color)

    # ── header + verdict, answer-first ──
    console.print()
    console.print(Text.assemble(
        ("  ▍", accent), ("archdrift", "bold"), ("  ·  ", "dim"),
        (service, "bold"), ("   ", ""), (profile, "dim on grey19")))
    console.print()
    console.print(Padding(Text.assemble(
        (f"{total}%", f"bold {accent}"), ("   ", ""),
        (grade, f"bold {accent}"), (f"   gate {threshold}%", "dim"),
        ("\n", ""), (audit.summary, "italic dim")), (0, 2)))
    console.print()

    # ── scorecard: three dimension bars inline ──
    card = Table.grid(padding=(0, 4, 0, 2))
    card.add_column(style="bold")
    card.add_column()
    for dim in Dim:
        card.add_row(dim.value, _bar(per_dim[dim]))
    console.print(Padding(card, (0, 2)))
    console.print()

    # ── every checked row, grouped by dimension (fail-first within each), passes dimmed ──
    for dim in Dim:
        rows = sorted((c for c in audit.checks if c.dimension is dim),
                      key=lambda c: (c.status is not Status.FAIL, c.status is not Status.WARN))
        if not rows:
            continue
        console.print(Padding(Text(dim.upper(), style="bold"), (0, 2)))
        tbl = Table.grid(padding=(0, 2, 0, 2))
        tbl.add_column(width=1)
        tbl.add_column(ratio=2, overflow="fold")
        tbl.add_column(ratio=3, style="dim", overflow="fold")
        for c in rows:
            mark, color = GLYPH[c.status]
            tbl.add_row(Text(mark, color),
                        Text(c.expected, "dim" if c.status is Status.PASS else color),
                        _polish(c.actual))
        console.print(Padding(tbl, (0, 2)))
        console.print()

    # ── undrawn pipelines, drawn as chains ──
    for title, hops in audit.undrawn_flows:
        chain = Text()
        chain.append(hops[0][0] if hops else "", "bold")
        for _, mechanism, target in hops:
            chain.append(f"  {mechanism} ▸ ", "yellow")
            chain.append(target, "bold")
        console.print(Padding(Text(f"⌁ UNDRAWN FLOW  {title}", style="yellow bold"), (0, 2)))
        console.print(Padding(chain, (0, 4)))
        console.print()

    # ── collapsed positives + operational notices ──
    verified = sum(c.status is Status.PASS for c in audit.checks)
    tail = [f"[green]✓ {verified} verified[/]"]
    if audit.dormant:
        tail.append(f"[dim]○ {len(audit.dormant)} dormant: {', '.join(audit.dormant)}[/]")
    if penalty:
        tail.append(f"[dim]drift −{penalty}[/]")
    console.print(Padding("   ·   ".join(tail), (0, 2)))
    scope = (f"[dim]scope {len(diagram.components)} components · {len(diagram.edges)} flows "
             f"· {len(diagram.steps)} steps[/]")
    console.print(Padding("   ·   ".join([scope, *notices]), (0, 2)))
    for anchor, suspect in diagram.loose_ends:
        where = (f"head sits on '{suspect}' — drag its endpoint onto it" if suspect
                 else "attached to nothing")
        console.print(Padding(f"[yellow]⚠ unglued arrow at '{anchor or '?'}': {where}[/]", (0, 2)))
    console.print()


# ── entrypoint ───────────────────────────────────────────────────────────────


def _env(key: str, fallback: str = "") -> str:
    # TODO: 12-factor config — environment-specific values come from ARCHDRIFT_* env vars.
    return os.environ.get(f"ARCHDRIFT_{key}", fallback)


def main() -> int:
    # TODO: pipeline — parse -> scope -> harvest -> consul -> audit -> score; exit 0/1 as CI gate.
    cli = argparse.ArgumentParser(
        description="archdrift: architecture match gate, HLD drawio vs microservice",
        epilog="every option also reads a ARCHDRIFT_* env var: "
               "MS, DIAGRAM, SERVICE, PROJECT, PROFILE, CONSUL_URL, THRESHOLD, MODEL")
    cli.add_argument("root", type=Path, nargs="?",
                     default=Path(v).expanduser() if (v := _env("MS")) else None,
                     help="microservice repo [env: ARCHDRIFT_MS]")
    cli.add_argument("diagram", type=Path, nargs="?",
                     default=Path(v).expanduser() if (v := _env("DIAGRAM")) else None,
                     help="HLD .drawio file [env: ARCHDRIFT_DIAGRAM]")
    cli.add_argument("--service", default=_env("SERVICE") or None,
                     help="defaults to the repo directory name")
    cli.add_argument("--project", default=_env("PROJECT"))
    cli.add_argument("--profile", default=_env("PROFILE", "preprod"))
    cli.add_argument("--consul-url", default=_env("CONSUL_URL") or None,
                     help="KV url template with {project}/{service}/{profile};"
                          " unset -> config match skipped")
    cli.add_argument("--redact", default=_env("REDACT"),
                     help="comma-separated identifiers to mask before sending, e.g. "
                          "'acmecorp,internal.host' or 'acmecorp=org1'")
    cli.add_argument("--no-consul", action="store_true")
    cli.add_argument("--threshold", type=int, default=int(_env("THRESHOLD", "70")))
    cli.add_argument("--model", default=_env("MODEL", "claude-sonnet-5"))
    args = cli.parse_args()

    console = Console()
    if not (args.root and args.root.is_dir() and args.diagram and args.diagram.is_file()):
        cli.error("microservice and diagram paths required — pass them as arguments "
                  "or export ARCHDRIFT_MS / ARCHDRIFT_DIAGRAM")
    service = args.service or args.root.resolve().name

    with console.status("[dim]parsing HLD → harvesting codebase → auditing…[/]"):
        diagram = scope_to_service(parse_diagram(args.diagram), service)
        if not diagram.components:
            sys.exit(f"'{service}' has no arrows in the diagram — check the box label "
                     f"or pass --service to match a drawn component.")
        sources = harvest_codebase(args.root)
        kv_url = args.consul_url and args.consul_url.format(
            project=args.project, service=service, profile=args.profile)
        consul = fetch_consul(kv_url) if kv_url and not args.no_consul else None
        not_bound, undrawn_names = topic_diff(diagram, sources, consul, service)
        config = _config_text(sources, consul)
        directions, urls = bound_topics(config), outbound_urls(config)
        schedulers = scheduler_names(sources)
        redact = redaction_map(args.redact) if args.redact else {}
        audit = run_audit(diagram, sources, consul, service, args.model, undrawn_names, redact)
        audit = enforce_hints(audit, undrawn_names, directions, schedulers, service)
        audit = cross_check_clients(audit, urls, sources, service)
        audit = replace(audit, checks=(
            *audit.checks,
            *cross_check_names(not_bound, undrawn_names),
            *cross_check_directions(diagram, sources, consul, service),
            *cross_check_steps(diagram),
            *cross_check_order(diagram, audit.code_step_order),
        ))
        audit = neutralize_placeholders(enforce_qualifiers(suppress_dormant(audit)))

    # operational notices fold into the report footer
    notices: list[str] = []
    if not args.no_consul:
        notices.append(
            "[green]consul ✓[/]" if consul
            else "[yellow]⚠ consul unreachable[/]" if kv_url
            else "[yellow]⚠ consul not configured[/]")
    total, per_dim, penalty = score(audit)
    render(console, service, diagram, audit, total, per_dim, penalty,
           args.threshold, args.profile, notices)
    return 0 if total >= args.threshold else 1


if __name__ == "__main__":
    sys.exit(main())
