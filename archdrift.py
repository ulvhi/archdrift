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
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text


# ── acceptance criteria ──────────────────────────────────────────────────────
# The AI supplies a verdict per drawn element; the score is computed here — the model never grades itself.


class Dim(StrEnum):
    COMPONENTS = "components"  # every drawn box exists in code; every active integration is drawn
    FLOWS = "flows"            # every drawn arrow's direction + mechanism + chain order
    BINDINGS = "bindings"      # bound identifiers (topics, Feign ids, DB names) equal the labels


class Status(StrEnum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"


WEIGHTS: dict[Dim, int] = {Dim.COMPONENTS: 40, Dim.FLOWS: 35, Dim.BINDINGS: 25}
DRIFT_UNIT_PENALTY = 5   # per undrawn integration / pipeline, capped
DRIFT_PENALTY_CAP = 20

CRITERIA = """\
You are an architecture conformance reviewer. Compare ONE Spring Boot
microservice's implementation (Java sources, build.gradle, application*.yml,
runtime KV config) against its High-Level Design diagram. Architecture is
exactly what a diagram can draw: components (boxes — the service, datastores,
topics, caches, schedulers, external systems and clients), flows (arrows —
their direction and mechanism), and bindings (the identifiers labels bind).

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
- binding_checks: one row per bound identifier you can verify (Kafka topic
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
[team-namespace]") is judged deterministically by the tool — do NOT mark a
component warn or fail on account of a namespace, and do not emit binding
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
        "binding_checks": {
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
    "required": ["summary", "component_verdicts", "flow_verdicts", "binding_checks",
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
class AiExchange:
    request_body: str
    response_body: str


@dataclass(slots=True, frozen=True)
class Audit:
    summary: str
    extra: tuple[str, ...]                                    # standalone active drift — scored
    dormant: tuple[str, ...]                                  # dead code — informational, never scored
    undrawn_flows: tuple[tuple[str, tuple[Hop, ...]], ...] = ()  # (title, hops)
    checks: tuple[Check, ...] = ()
    code_step_order: tuple[str, ...] = ()                    # step ids in the order the CODE runs them
    ai_exchanges: tuple[AiExchange, ...] = ()
    secrets_masked: int = 0                                  # credential values stubbed out of the payload


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
    # TODO: collect architecture artifacts — Java sources (minus DTOs/entities), gradle, base yml.
    # Profile ymls in the repo (application-local, -preprod) are a developer's machine, not runtime
    # truth: they carry localhost hosts and stale names. Consul is the only profile config we trust.
    skip_dirs = {"dto", "dtos", "entity", "entities", "domain", "model", "models", "build", "bin"}
    skip_suffixes = ("Dto.java", "Entity.java", "Request.java", "Response.java")

    def _architectural(p: Path) -> bool:
        # TODO: keep wiring/behavior classes, drop pure data shapes.
        return not (
            skip_dirs & {part.lower() for part in p.parts}
            or p.name.endswith(skip_suffixes)
        )

    java = sorted(p for p in root.rglob("src/main/java/**/*.java") if _architectural(p))
    config = [root / "build.gradle",
              *sorted(p for p in root.rglob("src/main/resources/**/application*.yml")
                      if p.stem == "application")]
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


SECRET_KEYS = ("password", "passwd", "pwd", "secret", "token", "credential", "passphrase",
               "apikey", "api-key", "api_key", "accesskey", "access-key", "access_key",
               "privatekey", "private-key", "keystore", "truststore", "salt")

SECRET_MASK = "${secret}"

SECRET_RE = re.compile(
    r"([\w.\-]*(?:" + "|".join(SECRET_KEYS) + r")[\w.\-]*\s*[:=](?!=)\s*['\"]?)"
    r"([^\s'\"#,;)}\]]+)", re.IGNORECASE)
KEEP_RE = re.compile(r"\$\{|^(?:https?|jdbc|mongodb|redis|amqp)|^(?:true|false|null|\d+)$"
                     r"|^[|>][-+]?\d*$",
                     re.IGNORECASE)
URI_CRED_RE = re.compile(r"(://[^\s:/@]+:)([^\s:/@]+)(?=@)")
PLACEHOLDER_RE = re.compile(r"\$\{")
PEM_RE = re.compile(r"-----BEGIN[^-]*-----.*?-----END[^-]*-----", re.DOTALL)


def scrub_secrets(text: str) -> tuple[str, int]:
    masked = 0

    def _mask(keep: re.Pattern):
        def _sub(m: re.Match) -> str:
            nonlocal masked
            if keep.search(m[2]):
                return m[0]
            masked += 1
            return m[1] + SECRET_MASK
        return _sub

    text = SECRET_RE.sub(_mask(KEEP_RE), text)
    text = URI_CRED_RE.sub(_mask(PLACEHOLDER_RE), text)
    text, pem_blocks = PEM_RE.subn(SECRET_MASK, text)
    return text, masked + pem_blocks


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
    for c in verdict.get("binding_checks", []):
        checks.append(Check(Dim.BINDINGS, Status(c["status"]), c["expected"], c["actual"]))
    return tuple(checks), unassessed


def run_audit(diagram: Diagram, sources: dict[str, str], consul: str | None, service: str,
              model: str, undrawn_hints: tuple[str, ...] = (),
              redact: dict[str, str] | None = None) -> Audit:
    # TODO: stream the evidence bundle to the model; reconcile its verdicts against the known scope.
    masked_counts: list[int] = []

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
        bundle, masked = scrub_secrets("\n\n".join(blocks))
        masked_counts.append(masked)
        return apply_map(bundle, redact) if redact else bundle

    client = anthropic.Anthropic()
    exchanges: list[AiExchange] = []

    def _ask(content: str) -> dict:
        # TODO: one structured call; surface API/parse failures as clean messages, not tracebacks.
        request = {
            "model": model,
            "max_tokens": 32_000,
            "system": apply_map(CRITERIA, redact) if redact else CRITERIA,
            "output_config": {"format": {"type": "json_schema", "schema": AUDIT_SCHEMA}},
            "messages": [{"role": "user", "content": content}],
        }
        request_body = json.dumps(request, ensure_ascii=False, indent=2)
        try:
            with client.messages.stream(**request) as stream:
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
        exchanges.append(AiExchange(request_body=request_body, response_body=body))
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
        ai_exchanges=tuple(exchanges),
        secrets_masked=max(masked_counts, default=0),
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
    # TODO: resolve the endpoint behind a client — config confirms the binding (which host a call
    # actually lands on), it does NOT discover integrations. Only http(s) values: a JDBC datasource,
    # a ${...} placeholder, or an auth endpoint is not a REST call.
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


CALL_RE = re.compile(r"\b([a-z]\w*?)(?:Client|Api|Gateway|Feign)\s*\.\s*\w+\s*\(")   # fluent calls wrap lines
GENERIC_RECEIVERS = {"web", "rest", "http", "feign", "oauth", "mongo", "kafka", "redis", "vault",
                     "discovery"}   # framework plumbing, never a named integration


def outbound_clients(sources: dict[str, str]) -> tuple[str, ...]:
    # TODO: outbound integrations are discovered in the CODE — every invoked *Client/*Api/*Gateway call
    # site, whether it is a local @FeignClient or a shared library client with no annotation here. A url
    # sitting in config with no call behind it is config hygiene, not architecture, and is not our job.
    return tuple(sorted(
        {tok.lower() for path, body in sources.items() if path.endswith(".java")
         for tok in CALL_RE.findall(body)} - GENERIC_RECEIVERS
    ))


def client_method(token: str, sources: dict[str, str]) -> str:
    # TODO: the business step is the invoked client method — e.g. AtlasClient.getNationalExchangeRate();
    # name it from the import (class) + the call site (method), since library clients have no local paths.
    tok = token.replace("-", "").lower()
    call_re = re.compile(rf"\b{re.escape(tok)}(?:client|api|gateway|feign)\s*\.\s*(\w+)\s*\(",
                         re.IGNORECASE)   # receiver is camelCase; the token is its normalised prefix
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
    # TODO: deterministic guarantee for outbound integrations (the model drops clients at random) —
    # every client invoked in code that the model never accounted for is undrawn drift, named by the
    # invoked method (the business step) and, when config resolves one, the endpoint it lands on.
    # accounted-for = a finding the reader actually SEES (check row, dormant, undrawn flow); the
    # model's loose "extra" prose does not qualify — a precise deterministic row replaces it.
    covered = " ".join((*audit.dormant,
                        *(f"{c.expected} {c.actual}" for c in audit.checks),
                        *(f"{s} {t}" for _, hops in audit.undrawn_flows for s, _, t in hops))).lower()

    def _endpoint(token: str) -> str:
        # TODO: binding side — the configured host+path for this client, blank when it is Vault-held
        # or simply never configured. A missing value hides the endpoint, never the integration.
        match = next((u for key, u in urls.items() if key.replace("-", "") in token
                      or token in key.replace("-", "")), "")
        return re.sub(r"^https?://", "", match).rstrip("/")

    flows, checks, extra = list(audit.undrawn_flows), list(audit.checks), list(audit.extra)
    for token in outbound_clients(sources):
        endpoint = _endpoint(token)
        host = endpoint.split("/")[0]
        if token in covered or (host and host in covered):
            continue                                 # model mapped it to a drawn component / reported it
        extra = [e for e in extra if token not in e.lower()]    # the precise row supersedes the prose
        step = client_method(token, sources) or token           # business step: the invoked method
        where = f" at {endpoint}" if endpoint else ""
        flows.append((f"{token} (undrawn)",
                      ((service, "REST", f"{step}  ·  {endpoint}" if endpoint else step),)))
        checks.append(Check(Dim.FLOWS, Status.FAIL, "(not in HLD)",
                            f"{service} calls {step}{where} — outbound call not drawn"))
    return replace(audit, undrawn_flows=tuple(flows), checks=tuple(checks), extra=tuple(extra))


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
    # TODO: render the deterministic topic diff as aggregated binding rows.
    return tuple(
        Check(Dim.BINDINGS, Status.FAIL, expected, f"{actual}: {', '.join(names)}")
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
    # TODO: a label's namespace qualifier ("[team-namespace]") is a deterministic check — the
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


DECL_RE = re.compile(r"\b([A-Z]\w*?(?:Client|Api|Gateway))\b")


def dead_clients(sources: dict[str, str]) -> tuple[str, ...]:
    # TODO: a client type that is declared or injected but whose methods are never called is dead code,
    # not an integration — an unwired bean draws no arrow. Name them so findings about them can be
    # dropped rather than scored as drift, whatever the model decided.
    invoked = outbound_clients(sources)
    declared = {cls for path, body in sources.items() if path.endswith(".java")
                for cls in DECL_RE.findall(body)}
    return tuple(sorted(
        cls for cls in declared
        if (stem := re.sub(r"(?:Client|Api|Gateway)$", "", cls).lower())
        and stem not in GENERIC_RECEIVERS
        and not any(tok in stem or stem in tok for tok in invoked)
    ))


def suppress_dead_clients(audit: Audit, sources: dict[str, str]) -> Audit:
    # TODO: drop every finding that names a dead client and record it as dormant instead, so the
    # "dead code is never a penalty" rule holds on any model tier rather than by the model's goodwill.
    if not (dead := dead_clients(sources)):
        return audit
    stems = tuple(re.sub(r"(?:Client|Api|Gateway)$", "", c).lower() for c in dead)

    def _names(text: str) -> bool:
        # TODO: does this finding talk about a client nothing actually calls?
        return any(stem in text.lower() for stem in stems)

    return replace(
        audit,
        dormant=(*audit.dormant, *(f"{c} (declared, never called)" for c in dead)),
        extra=tuple(e for e in audit.extra if not _names(e)),
        checks=tuple(c for c in audit.checks
                     if c.status is Status.PASS or not _names(f"{c.expected} {c.actual}")),
        undrawn_flows=tuple(
            fl for fl in audit.undrawn_flows
            if not _names(f"{fl[0]} " + " ".join(" ".join(hop) for hop in fl[1]))),
    )


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

    # ── standalone undrawn integrations: scored, so never silent ──
    for item in audit.extra:
        console.print(Padding(Text.assemble(("⌁ UNDRAWN  ", "yellow bold"), (item, "yellow")), (0, 2)))
    if audit.extra:
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


def render_ai_io(console: Console, exchanges: tuple[AiExchange, ...]) -> None:
    # TODO: opt-in audit trail — show exactly what crossed the model boundary, not the locally
    # restored report. Multiple calls (the corrective retry) stay inside the same two blocks.
    def _document(attribute: str, label: str) -> str:
        bodies = []
        for number, exchange in enumerate(exchanges, 1):
            raw = getattr(exchange, attribute)
            try:
                body = json.loads(raw)
            except json.JSONDecodeError:
                body = raw
            bodies.append({"call": number, label: body})
        value = bodies[0][label] if len(bodies) == 1 else bodies
        return json.dumps(value, ensure_ascii=False, indent=2)

    console.rule("[bold cyan]MODEL I/O AUDIT[/]")
    console.print(Padding(
        "[dim]Full payloads may contain source and runtime config. Keep --show-ai-io out of shared CI logs.[/]",
        (0, 2, 1, 2),
    ))
    console.print(Panel(
        Syntax(_document("request_body", "request"), "json", theme="ansi_dark",
               word_wrap=True, background_color="default"),
        title="[bold cyan]REQUEST BODY · outbound after --redact[/]",
        border_style="cyan",
        padding=(1, 1),
    ))
    console.print(Panel(
        Syntax(_document("response_body", "response"), "json", theme="ansi_dark",
               word_wrap=True, background_color="default"),
        title="[bold magenta]RESPONSE BODY · raw before local name restore[/]",
        border_style="magenta",
        padding=(1, 1),
    ))
    console.print()


def render_html(path: Path, service: str, diagram: Diagram, audit: Audit, total: int,
                per_dim: dict[Dim, int | None], penalty: int, threshold: int,
                profile: str, notices: list[str]) -> None:
    # TODO: the ArchCom-facing artifact — the exact same report as the terminal, as ONE self-contained
    # HTML file (inline CSS, no CDN, no requests: nothing leaves the machine) that a CI job attaches
    # to the merge request so reviewers click a link instead of reading terminal scrollback.
    esc = html.escape
    passed = total >= threshold
    grade = ("CLEAN" if total >= 90 else "PASSING" if passed
             else "DRIFTING" if total >= 50 else "BROKEN")
    ICON = {Status.PASS: ("●", "ok"), Status.WARN: ("▲", "warn"), Status.FAIL: ("✕", "fail")}

    def _meter(dim: Dim) -> str:
        # TODO: one magnitude bar per dimension — value label always beside it, never color alone.
        if (v := per_dim[dim]) is None:
            return (f'<div class="meter"><span class="mlab">{dim.value}</span>'
                    f'<div class="track"></div><span class="mval">—</span></div>')
        cls = "ok" if v >= 80 else "warn" if v >= 50 else "fail"
        return (f'<div class="meter"><span class="mlab">{dim.value}</span>'
                f'<div class="track"><div class="fill {cls}" style="width:{v}%"></div></div>'
                f'<span class="mval">{v}%</span></div>')

    def _section(dim: Dim) -> str:
        # TODO: same ordering discipline as the terminal — failures first, passes recede.
        rows = sorted((c for c in audit.checks if c.dimension is dim),
                      key=lambda c: (c.status is not Status.FAIL, c.status is not Status.WARN))
        if not rows:
            return ""
        body = "\n".join(
            f'<tr><td class="ic {ICON[c.status][1]}">{ICON[c.status][0]}</td>'
            f'<td class="exp{"" if c.status is not Status.PASS else " dim"}">{esc(c.expected)}</td>'
            f'<td class="act">{esc(_polish(c.actual))}</td></tr>'
            for c in rows)
        return f'<h2>{dim.value}</h2><table>{body}</table>'

    def _chain(title: str, hops: tuple[Hop, ...]) -> str:
        # TODO: an undrawn pipeline reads as the chain of component hops the diagram is missing.
        parts = [f'<span class="pill">{esc(hops[0][0])}</span>'] if hops else []
        parts += [f'<span class="mech">{esc(mech)} ▸</span><span class="pill">{esc(target)}</span>'
                  for _, mech, target in hops]
        return (f'<div class="flow"><div class="ftitle">⌁ undrawn flow · {esc(title)}</div>'
                f'<div class="chain">{"".join(parts)}</div></div>')

    verified = sum(c.status is Status.PASS for c in audit.checks)
    plain_notices = [re.sub(r"\[/?[^\]]*\]", "", n) for n in notices]
    footer = "  ·  ".join(filter(None, (
        f"✓ {verified} verified",
        f"○ {len(audit.dormant)} dormant: {esc(', '.join(audit.dormant))}" if audit.dormant else "",
        f"drift −{penalty}" if penalty else "",
        f"scope {len(diagram.components)} components · {len(diagram.edges)} flows "
        f"· {len(diagram.steps)} steps",
        *map(esc, plain_notices))))
    extras = "\n".join(f'<div class="extra">⌁ {esc(item)}</div>' for item in audit.extra)
    flows = "\n".join(_chain(t, h) for t, h in audit.undrawn_flows)

    path.write_text(f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>archdrift · {esc(service)}</title><style>
:root{{color-scheme:light;--page:#f9f9f7;--card:#fcfcfb;--ink:#0b0b0b;--ink2:#52514e;
--muted:#898781;--hairline:#e1e0d9;--ring:rgba(11,11,11,.10);
--ok:#0ca30c;--warn:#fab219;--fail:#d03b3b;--track:#eceae4}}
@media (prefers-color-scheme:dark){{:root{{color-scheme:dark;--page:#0d0d0d;--card:#1a1a19;
--ink:#fff;--ink2:#c3c2b7;--hairline:#2c2c2a;--ring:rgba(255,255,255,.10);--track:#2c2c2a}}}}
*{{box-sizing:border-box;margin:0}}
body{{background:var(--page);color:var(--ink);font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;padding:32px 16px}}
main{{max-width:1080px;margin:auto;background:var(--card);border:1px solid var(--ring);border-radius:12px;padding:32px 36px}}
header{{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap}}
header .svc{{font-weight:700}} header .prof,header .tool{{color:var(--muted);font-size:13px}}
.hero{{display:flex;align-items:baseline;gap:14px;margin:18px 0 4px;flex-wrap:wrap}}
.hero .score{{font-size:44px;font-weight:700;color:var(--{'ok' if passed else 'fail'})}}
.hero .grade{{font-weight:700;font-size:18px;color:var(--{'ok' if passed else 'fail'})}}
.summary{{color:var(--ink2);max-width:70ch;margin-bottom:22px}}
.meter{{display:grid;grid-template-columns:110px 1fr 52px;gap:12px;align-items:center;margin:6px 0}}
.mlab{{font-weight:600;font-size:13px}} .mval{{font-variant-numeric:tabular-nums;text-align:right;font-size:13px;color:var(--ink2)}}
.track{{height:8px;border-radius:4px;background:var(--track);overflow:hidden}}
.fill{{height:100%;border-radius:4px}} .fill.ok{{background:var(--ok)}} .fill.warn{{background:var(--warn)}} .fill.fail{{background:var(--fail)}}
h2{{font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);margin:26px 0 8px}}
table{{width:100%;border-collapse:collapse}} td{{padding:6px 10px 6px 0;vertical-align:top;border-top:1px solid var(--hairline)}}
td.ic{{width:22px;font-size:12px}} .ic.ok{{color:var(--ok)}} .ic.warn{{color:var(--warn)}} .ic.fail{{color:var(--fail)}}
td.exp{{width:42%;font-weight:500}} td.exp.dim{{font-weight:400}} td.act{{color:var(--ink2)}}
.flow{{margin:14px 0}} .ftitle{{font-weight:600;color:var(--warn);font-size:13px;margin-bottom:6px}}
.extra{{color:var(--warn);font-size:13px;margin:6px 0}}
.chain{{display:flex;align-items:center;gap:8px;flex-wrap:wrap}}
.pill{{border:1px solid var(--hairline);border-radius:6px;padding:2px 10px;font-size:13px;font-weight:500}}
.mech{{color:var(--muted);font-size:12px}}
footer{{margin-top:28px;padding-top:14px;border-top:1px solid var(--hairline);color:var(--muted);font-size:13px}}
</style></head><body><main>
<header><span class="tool">archdrift</span><span class="svc">{esc(service)}</span>
<span class="prof">{esc(profile)}</span></header>
<div class="hero"><span class="score">{total}%</span><span class="grade">{grade}</span></div>
<p class="summary">{esc(audit.summary)}</p>
{"".join(_meter(d) for d in Dim)}
{"".join(_section(d) for d in Dim)}
{f'<h2>undrawn</h2>{flows}{extras}' if flows or extras else ''}
<footer>{footer}</footer>
</main></body></html>""", encoding="utf-8")


# ── entrypoint ───────────────────────────────────────────────────────────────


def _env(key: str, fallback: str = "") -> str:
    # TODO: 12-factor config — environment-specific values come from ARCHDRIFT_* env vars.
    return os.environ.get(f"ARCHDRIFT_{key}", fallback)


def run_gate(root: Path, diagram_path: Path, *, service: str | None = None, project: str = "",
             profile: str = "preprod", consul_url: str | None = None,
             model: str = "claude-sonnet-5", redact_spec: str = "", no_consul: bool = False,
             ) -> tuple[str, Diagram, Audit, int, dict[Dim, int | None], int, list[str]]:
    # TODO: the whole gate as ONE callable — the CLI and the web UI share this pipeline verbatim,
    # so a browser run can never drift from what CI enforces.
    service = service or root.resolve().name
    diagram = scope_to_service(parse_diagram(diagram_path), service)
    if not diagram.components:
        raise ValueError(f"'{service}' has no arrows in the diagram — check the box label "
                         f"or pass a service name that matches a drawn component.")
    sources = harvest_codebase(root)
    kv_url = consul_url and consul_url.format(project=project, service=service, profile=profile)
    consul = fetch_consul(kv_url) if kv_url and not no_consul else None
    not_bound, undrawn_names = topic_diff(diagram, sources, consul, service)
    config = _config_text(sources, consul)
    directions, urls = bound_topics(config), outbound_urls(config)
    schedulers = scheduler_names(sources)
    redact = redaction_map(redact_spec) if redact_spec else {}
    audit = run_audit(diagram, sources, consul, service, model, undrawn_names, redact)
    audit = enforce_hints(audit, undrawn_names, directions, schedulers, service)
    audit = cross_check_clients(audit, urls, sources, service)
    audit = replace(audit, checks=(
        *audit.checks,
        *cross_check_names(not_bound, undrawn_names),
        *cross_check_directions(diagram, sources, consul, service),
        *cross_check_steps(diagram),
        *cross_check_order(diagram, audit.code_step_order),
    ))
    audit = suppress_dead_clients(audit, sources)
    audit = neutralize_placeholders(enforce_qualifiers(suppress_dormant(audit)))

    notices: list[str] = []
    if audit.secrets_masked:
        notices.append(f"[dim]🔒 {audit.secrets_masked} secrets masked[/]")
    if not no_consul:
        notices.append(
            "[green]consul ✓[/]" if consul
            else "[yellow]⚠ consul unreachable[/]" if kv_url
            else "[yellow]⚠ consul not configured[/]")
    total, per_dim, penalty = score(audit)
    return service, diagram, audit, total, per_dim, penalty, notices


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
    cli.add_argument("--show-ai-io", action="store_true",
                     help="print the full redacted model request and raw response at the end; "
                          "may expose source/config in terminal or CI logs")
    cli.add_argument("--html", type=Path, default=Path(p) if (p := _env("HTML")) else None,
                     help="also write the report as one self-contained HTML file (CI artifact)")
    cli.add_argument("--no-consul", action="store_true")
    cli.add_argument("--threshold", type=int, default=int(_env("THRESHOLD", "70")))
    cli.add_argument("--model", default=_env("MODEL", "claude-sonnet-5"))
    args = cli.parse_args()

    console = Console()
    if not (args.root and args.root.is_dir() and args.diagram and args.diagram.is_file()):
        cli.error("microservice and diagram paths required — pass them as arguments "
                  "or export ARCHDRIFT_MS / ARCHDRIFT_DIAGRAM")
    with console.status("[dim]parsing HLD → harvesting codebase → auditing…[/]"):
        try:
            service, diagram, audit, total, per_dim, penalty, notices = run_gate(
                args.root, args.diagram, service=args.service, project=args.project,
                profile=args.profile, consul_url=args.consul_url, model=args.model,
                redact_spec=args.redact or "", no_consul=args.no_consul)
        except ValueError as err:
            sys.exit(str(err))
    render(console, service, diagram, audit, total, per_dim, penalty,
           args.threshold, args.profile, notices)
    if args.html:
        render_html(args.html, service, diagram, audit, total, per_dim, penalty,
                    args.threshold, args.profile, notices)
        console.print(Padding(f"[dim]html report → {args.html}[/]", (0, 2)))
    if args.show_ai_io:
        render_ai_io(console, audit.ai_exchanges)
    return 0 if total >= args.threshold else 1


if __name__ == "__main__":
    sys.exit(main())
