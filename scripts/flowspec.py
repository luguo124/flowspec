#!/usr/bin/env python3
"""Validate and render FlowSpec JSON using only the Python standard library."""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections import defaultdict, deque
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable


NODE_TYPES = {"start", "action", "decision", "external", "state", "end"}
EDGE_KINDS = {"normal", "error", "timeout", "cancel", "retry", "fallback"}
DIAGRAM_TYPES = {"flowchart", "state"}
DIRECTIONS = {"TD", "TB", "LR", "RL", "BT"}
TEST_CATEGORIES = {"happy", "edge", "error", "state", "recovery"}
SPEC_STATUSES = {"draft", "review_ready", "verified"}
ID_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")


@dataclass(frozen=True)
class Issue:
    severity: str
    code: str
    message: str
    refs: tuple[str, ...] = ()

    def line(self) -> str:
        suffix = f" [{', '.join(self.refs)}]" if self.refs else ""
        return f"{self.severity.upper():7} {self.code}: {self.message}{suffix}"


def load_spec(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"File not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}"
        ) from exc
    if not isinstance(data, dict):
        raise ValueError("The FlowSpec root must be a JSON object")
    return data


def _list(spec: dict[str, Any], key: str, issues: list[Issue]) -> list[dict[str, Any]]:
    value = spec.get(key, [])
    if not isinstance(value, list):
        issues.append(Issue("error", "invalid_collection", f"'{key}' must be an array"))
        return []
    result: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            issues.append(
                Issue("error", "invalid_item", f"'{key}[{index}]' must be an object")
            )
            continue
        result.append(item)
    return result


def _index(
    items: list[dict[str, Any]], collection: str, issues: list[Issue]
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(items):
        item_id = item.get("id")
        if not isinstance(item_id, str) or not item_id.strip():
            issues.append(
                Issue(
                    "error",
                    "missing_id",
                    f"'{collection}[{index}]' must have a non-empty string id",
                )
            )
            continue
        if not ID_PATTERN.fullmatch(item_id):
            issues.append(
                Issue(
                    "error",
                    "invalid_id",
                    "IDs must start with an ASCII letter and contain only letters, digits, '_' or '-'",
                    (item_id,),
                )
            )
        if item_id in result:
            issues.append(
                Issue("error", "duplicate_id", f"Duplicate id in '{collection}'", (item_id,))
            )
            continue
        result[item_id] = item
    return result


def _refs(
    item: dict[str, Any],
    field: str,
    allowed: set[str],
    issues: list[Issue],
    owner: str,
) -> None:
    values = item.get(field, [])
    if values is None:
        return
    if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
        issues.append(
            Issue("error", "invalid_references", f"'{field}' must be an array of IDs", (owner,))
        )
        return
    missing = [value for value in values if value not in allowed]
    if missing:
        issues.append(
            Issue(
                "error",
                "unknown_reference",
                f"'{field}' contains unknown IDs: {', '.join(missing)}",
                (owner,),
            )
        )


def _reachable(starts: Iterable[str], adjacency: dict[str, list[str]]) -> set[str]:
    seen: set[str] = set()
    queue: deque[str] = deque(starts)
    while queue:
        node = queue.popleft()
        if node in seen:
            continue
        seen.add(node)
        queue.extend(adjacency.get(node, []))
    return seen


def _strongly_connected_components(
    node_ids: Iterable[str], adjacency: dict[str, list[str]]
) -> list[set[str]]:
    index = 0
    stack: list[str] = []
    on_stack: set[str] = set()
    indices: dict[str, int] = {}
    lowlink: dict[str, int] = {}
    components: list[set[str]] = []

    def visit(node: str) -> None:
        nonlocal index
        indices[node] = index
        lowlink[node] = index
        index += 1
        stack.append(node)
        on_stack.add(node)

        for target in adjacency.get(node, []):
            if target not in indices:
                visit(target)
                lowlink[node] = min(lowlink[node], lowlink[target])
            elif target in on_stack:
                lowlink[node] = min(lowlink[node], indices[target])

        if lowlink[node] == indices[node]:
            component: set[str] = set()
            while True:
                member = stack.pop()
                on_stack.remove(member)
                component.add(member)
                if member == node:
                    break
            components.append(component)

    for node_id in node_ids:
        if node_id not in indices:
            visit(node_id)
    return components


def validate(spec: dict[str, Any]) -> list[Issue]:
    issues: list[Issue] = []

    for field in ("version", "title", "diagram_type", "nodes", "edges"):
        if field not in spec:
            issues.append(Issue("error", "missing_field", f"Missing required field '{field}'"))
    if not isinstance(spec.get("version"), str) or not spec.get("version", "").strip():
        issues.append(Issue("error", "invalid_version", "'version' must be a non-empty string"))
    if not isinstance(spec.get("title"), str) or not spec.get("title", "").strip():
        issues.append(Issue("error", "invalid_title", "'title' must be a non-empty string"))
    status = spec.get("status", "draft")
    if status not in SPEC_STATUSES:
        issues.append(
            Issue(
                "error",
                "invalid_status",
                "'status' must be 'draft', 'review_ready', or 'verified'",
            )
        )
    diagram_type = spec.get("diagram_type")
    if diagram_type not in DIAGRAM_TYPES:
        issues.append(
            Issue("error", "invalid_diagram_type", "'diagram_type' must be 'flowchart' or 'state'")
        )
    direction = spec.get("direction", "TD")
    if direction not in DIRECTIONS:
        issues.append(
            Issue("error", "invalid_direction", f"Unsupported direction '{direction}'")
        )

    actors = _list(spec, "actors", issues)
    sources = _list(spec, "sources", issues)
    facts = _list(spec, "facts", issues)
    assumptions = _list(spec, "assumptions", issues)
    questions = _list(spec, "questions", issues)
    nodes = _list(spec, "nodes", issues)
    edges = _list(spec, "edges", issues)
    criteria = _list(spec, "acceptance_criteria", issues)
    tests = _list(spec, "test_scenarios", issues)

    actor_map = _index(actors, "actors", issues)
    source_map = _index(sources, "sources", issues)
    fact_map = _index(facts, "facts", issues)
    assumption_map = _index(assumptions, "assumptions", issues)
    _index(questions, "questions", issues)
    node_map = _index(nodes, "nodes", issues)
    edge_map = _index(edges, "edges", issues)
    criterion_map = _index(criteria, "acceptance_criteria", issues)
    test_map = _index(tests, "test_scenarios", issues)

    for source_id, source in source_map.items():
        if not source.get("kind") or not source.get("ref"):
            issues.append(
                Issue(
                    "warning",
                    "incomplete_source",
                    "A source should include both 'kind' and 'ref'",
                    (source_id,),
                )
            )

    for fact_id, fact in fact_map.items():
        _refs(fact, "source_ids", set(source_map), issues, fact_id)
        if not fact.get("source_ids"):
            issues.append(
                Issue("warning", "untraced_fact", "Confirmed fact has no source", (fact_id,))
            )

    for assumption_id, assumption in assumption_map.items():
        _refs(assumption, "source_ids", set(source_map), issues, assumption_id)
        if assumption.get("status", "unverified") != "verified" and not assumption.get("verification"):
            issues.append(
                Issue(
                    "warning",
                    "assumption_without_verification",
                    "Unverified assumption has no verification plan",
                    (assumption_id,),
                )
            )
        if status == "verified" and assumption.get("status", "unverified") != "verified":
            issues.append(
                Issue(
                    "error",
                    "unverified_assumption_in_verified_spec",
                    "A verified spec cannot contain an unverified assumption",
                    (assumption_id,),
                )
            )

    for question in questions:
        question_id = str(question.get("id", "?"))
        _refs(question, "source_ids", set(source_map), issues, question_id)
        if question.get("blocking") is True:
            issues.append(
                Issue(
                    "error" if status in {"review_ready", "verified"} else "warning",
                    "blocking_question_open",
                    str(question.get("question", "Blocking question remains open")),
                    (question_id,),
                )
            )

    for node_id, node in node_map.items():
        node_type = node.get("type")
        if node_type not in NODE_TYPES:
            issues.append(
                Issue("error", "invalid_node_type", f"Unsupported node type '{node_type}'", (node_id,))
            )
        if not isinstance(node.get("label"), str) or not node.get("label", "").strip():
            issues.append(Issue("error", "missing_node_label", "Node needs a label", (node_id,)))
        actor_id = node.get("actor_id")
        if actor_id is not None and actor_id not in actor_map:
            issues.append(
                Issue("error", "unknown_actor", f"Unknown actor '{actor_id}'", (node_id,))
            )
        _refs(node, "source_ids", set(source_map), issues, node_id)
        _refs(node, "fact_ids", set(fact_map), issues, node_id)
        _refs(node, "assumption_ids", set(assumption_map), issues, node_id)
        _refs(node, "acceptance_ids", set(criterion_map), issues, node_id)

    adjacency: dict[str, list[str]] = defaultdict(list)
    reverse: dict[str, list[str]] = defaultdict(list)
    incoming: dict[str, list[dict[str, Any]]] = defaultdict(list)
    outgoing: dict[str, list[dict[str, Any]]] = defaultdict(list)
    edge_pairs: set[tuple[str, str]] = set()

    for edge_id, edge in edge_map.items():
        source = edge.get("from")
        target = edge.get("to")
        if source not in node_map:
            issues.append(
                Issue("error", "unknown_edge_source", f"Unknown source node '{source}'", (edge_id,))
            )
        if target not in node_map:
            issues.append(
                Issue("error", "unknown_edge_target", f"Unknown target node '{target}'", (edge_id,))
            )
        kind = edge.get("kind", "normal")
        if kind not in EDGE_KINDS:
            issues.append(
                Issue("error", "invalid_edge_kind", f"Unsupported edge kind '{kind}'", (edge_id,))
            )
        if kind == "retry" and not (edge.get("limit") or edge.get("stop_policy")):
            issues.append(
                Issue(
                    "warning",
                    "unbounded_retry",
                    "Retry edge needs 'limit' or 'stop_policy'",
                    (edge_id,),
                )
            )
        _refs(edge, "source_ids", set(source_map), issues, edge_id)
        _refs(edge, "fact_ids", set(fact_map), issues, edge_id)
        _refs(edge, "assumption_ids", set(assumption_map), issues, edge_id)
        if (
            diagram_type == "state"
            and source in node_map
            and target in node_map
            and node_map[source].get("type") == "state"
            and node_map[target].get("type") == "state"
            and not edge.get("event")
        ):
            issues.append(
                Issue(
                    "warning",
                    "state_transition_without_event",
                    "State-to-state transition should declare an event",
                    (edge_id,),
                )
            )
        if source in node_map and target in node_map:
            adjacency[source].append(target)
            reverse[target].append(source)
            outgoing[source].append(edge)
            incoming[target].append(edge)
            edge_pairs.add((source, target))

    starts = [node_id for node_id, node in node_map.items() if node.get("type") == "start"]
    ends = [node_id for node_id, node in node_map.items() if node.get("type") == "end"]
    if len(starts) != 1:
        issues.append(
            Issue("error", "start_count", f"Expected exactly one start node, found {len(starts)}")
        )
    if not ends:
        issues.append(Issue("error", "end_count", "Expected at least one end node"))

    for start in starts:
        if incoming.get(start):
            issues.append(Issue("error", "start_has_incoming", "Start node has incoming edges", (start,)))
    for end in ends:
        if outgoing.get(end):
            issues.append(Issue("error", "end_has_outgoing", "End node has outgoing edges", (end,)))

    for node_id, node in node_map.items():
        node_type = node.get("type")
        node_edges = outgoing.get(node_id, [])
        if node_type != "end" and not node_edges:
            issues.append(
                Issue("error", "nonterminal_sink", "Non-end node has no outgoing edge", (node_id,))
            )
        if node_type == "decision":
            if len(node_edges) < 2:
                issues.append(
                    Issue("error", "decision_branch_count", "Decision needs at least two outgoing edges", (node_id,))
                )
            labels = [str(edge.get("condition", "")).strip() for edge in node_edges]
            if any(not label for label in labels):
                issues.append(
                    Issue("error", "unlabeled_decision_branch", "Every decision branch needs a condition", (node_id,))
                )
            normalized = [label.casefold() for label in labels if label]
            if len(normalized) != len(set(normalized)):
                issues.append(
                    Issue("error", "duplicate_decision_branch", "Decision branch labels must be distinct", (node_id,))
                )
            defaults = [edge for edge in node_edges if edge.get("is_default") is True]
            if len(defaults) > 1:
                issues.append(
                    Issue("error", "multiple_default_branches", "Decision has more than one default branch", (node_id,))
                )
            truth_pairs = (
                {"yes", "no"},
                {"true", "false"},
                {"是", "否"},
                {"成功", "失败"},
                {"通过", "不通过"},
            )
            if labels and not defaults and set(normalized) not in truth_pairs:
                issues.append(
                    Issue(
                        "warning",
                        "decision_exhaustiveness_unproven",
                        "Branch labels are not a recognized binary pair and no default branch is declared; review completeness manually",
                        (node_id,),
                    )
                )
        elif len(node_edges) > 1 and all(not edge.get("condition") for edge in node_edges):
            issues.append(
                Issue(
                    "warning",
                    "implicit_branch",
                    "Non-decision node has multiple unlabeled outgoing edges",
                    (node_id,),
                )
            )
        if node_type == "external":
            failure_kinds = {edge.get("kind", "normal") for edge in node_edges}
            if not failure_kinds.intersection({"error", "timeout", "fallback"}):
                issues.append(
                    Issue(
                        "warning",
                        "external_failure_path_missing",
                        "External call has no explicit error, timeout, or fallback edge",
                        (node_id,),
                    )
                )

    if len(starts) == 1:
        reached = _reachable(starts, adjacency)
        for node_id in sorted(set(node_map) - reached):
            issues.append(Issue("error", "unreachable_node", "Node is unreachable from start", (node_id,)))
    if ends:
        can_finish = _reachable(ends, reverse)
        for node_id in sorted(set(node_map) - can_finish):
            issues.append(
                Issue("error", "no_path_to_end", "Node cannot reach any end node", (node_id,))
            )

    if diagram_type == "flowchart":
        for component in _strongly_connected_components(node_map, adjacency):
            cyclic = len(component) > 1 or any(
                source == target and source in component for source, target in edge_pairs
            )
            if not cyclic:
                continue
            exits = [
                edge
                for member in component
                for edge in outgoing.get(member, [])
                if edge.get("to") not in component
            ]
            refs = tuple(sorted(component))
            if not exits:
                issues.append(
                    Issue("error", "loop_without_exit", "Cycle has no exit edge", refs)
                )
            elif all(not edge.get("condition") and not edge.get("is_default") for edge in exits):
                issues.append(
                    Issue(
                        "warning",
                        "loop_exit_unclear",
                        "Cycle has an exit, but its exit condition is not explicit",
                        refs,
                    )
                )

    accepted_nodes: set[str] = set()
    accepted_edges: set[str] = set()
    for criterion_id, criterion in criterion_map.items():
        _refs(criterion, "node_ids", set(node_map), issues, criterion_id)
        _refs(criterion, "edge_ids", set(edge_map), issues, criterion_id)
        accepted_nodes.update(
            node_id for node_id in criterion.get("node_ids", []) if node_id in node_map
        )
        accepted_edges.update(
            edge_id for edge_id in criterion.get("edge_ids", []) if edge_id in edge_map
        )
        if not criterion.get("given") or not criterion.get("when") or not criterion.get("then"):
            issues.append(
                Issue(
                    "warning",
                    "incomplete_acceptance_criterion",
                    "Acceptance criterion should include given, when, and then",
                    (criterion_id,),
                )
            )

    for node_id, node in node_map.items():
        if node.get("type") in {"action", "decision", "external", "state"}:
            if node_id not in accepted_nodes and not node.get("acceptance_ids"):
                issues.append(
                    Issue(
                        "warning",
                        "node_without_acceptance",
                        "Key node is not linked to an acceptance criterion",
                        (node_id,),
                    )
                )

    for edge_id, edge in edge_map.items():
        source = edge.get("from")
        source_type = node_map.get(str(source), {}).get("type")
        kind = edge.get("kind", "normal")
        if (
            source_type == "decision"
            or kind in {"error", "timeout", "cancel", "retry", "fallback"}
        ) and edge_id not in accepted_edges:
            issues.append(
                Issue(
                    "warning",
                    "branch_without_acceptance",
                    "Decision or failure branch is not directly linked to an acceptance criterion",
                    (edge_id,),
                )
            )

    covered_criteria: set[str] = set()
    for test_id, test in test_map.items():
        _refs(test, "covers", set(criterion_map), issues, test_id)
        _refs(test, "path", set(node_map), issues, test_id)
        covered_criteria.update(
            criterion_id for criterion_id in test.get("covers", []) if criterion_id in criterion_map
        )
        category = test.get("category")
        if category not in TEST_CATEGORIES:
            issues.append(
                Issue(
                    "warning",
                    "unknown_test_category",
                    f"Use one of: {', '.join(sorted(TEST_CATEGORIES))}",
                    (test_id,),
                )
            )
        path = test.get("path", [])
        if isinstance(path, list) and path and all(isinstance(item, str) for item in path):
            for source, target in zip(path, path[1:]):
                if source in node_map and target in node_map and (source, target) not in edge_pairs:
                    issues.append(
                        Issue(
                            "error",
                            "invalid_test_path",
                            f"No edge exists from '{source}' to '{target}'",
                            (test_id,),
                        )
                    )
            if starts and path[0] not in starts:
                issues.append(
                    Issue("error", "test_path_missing_start", "Test path must begin at start", (test_id,))
                )
            if ends and path[-1] not in ends:
                issues.append(
                    Issue("error", "test_path_missing_end", "Test path must end at an end node", (test_id,))
                )
        else:
            issues.append(
                Issue("warning", "test_without_path", "Test scenario should declare a complete path", (test_id,))
            )
        for field in ("preconditions", "steps", "expected"):
            if not test.get(field):
                issues.append(
                    Issue(
                        "warning",
                        "incomplete_test_scenario",
                        f"Test scenario is missing '{field}'",
                        (test_id,),
                    )
                )

    for criterion_id in sorted(set(criterion_map) - covered_criteria):
        issues.append(
            Issue(
                "warning",
                "acceptance_without_test",
                "Acceptance criterion is not covered by a test scenario",
                (criterion_id,),
            )
        )

    return issues


def _text(value: Any) -> str:
    return str(value).replace("\\", "/").replace('"', "'").replace("\n", "<br/>")


def _edge_label(edge: dict[str, Any], state: bool = False) -> str:
    if state:
        parts: list[str] = []
        if edge.get("event"):
            parts.append(str(edge["event"]))
        if edge.get("guard"):
            parts.append(f"[{edge['guard']}]")
        if edge.get("effect"):
            parts.append(f"/ {edge['effect']}")
        label = " ".join(parts) or str(edge.get("condition", ""))
    else:
        label = str(edge.get("condition", ""))
    kind = edge.get("kind", "normal")
    if kind != "normal":
        label = f"{kind}: {label}" if label else kind
    return _text(label).replace("|", "/")


def drawio_id(kind: str, raw: str) -> str:
    """Return a stable draw.io-safe ID in a namespace for one cell kind."""
    normalized = re.sub(r"[^A-Za-z0-9_.-]", "_", raw).strip("_") or "item"
    return f"{kind}-{normalized}"


def _drawio_html(value: Any) -> str:
    return html.escape(str(value), quote=False).replace("\n", "<br>")


def _drawio_label(label: Any, item_id: str) -> str:
    return (
        f"<div>{_drawio_html(label)}</div>"
        f'<div><font color="#6B7280" style="font-size:10px">'
        f"{_drawio_html(item_id)}</font></div>"
    )


def _drawio_tooltip(item: dict[str, Any]) -> str:
    lines = [str(item.get("id", ""))]
    for field, title in (
        ("actor_id", "责任角色"),
        ("source_ids", "来源"),
        ("fact_ids", "事实"),
        ("assumption_ids", "假设"),
        ("acceptance_ids", "验收"),
    ):
        value = item.get(field)
        if isinstance(value, list):
            value = ", ".join(str(entry) for entry in value)
        if value:
            lines.append(f"{title}: {value}")
    return "\n".join(lines)


def _drawio_metadata(item: dict[str, Any], item_type: str) -> dict[str, str]:
    result = {
        "flowspecId": str(item.get("id", "")),
        "flowspecType": item_type,
    }
    for source, target in (
        ("actor_id", "actorId"),
        ("source_ids", "sourceIds"),
        ("fact_ids", "factIds"),
        ("assumption_ids", "assumptionIds"),
        ("acceptance_ids", "acceptanceIds"),
    ):
        value = item.get(source)
        if isinstance(value, list):
            value = ",".join(str(entry) for entry in value)
        if value:
            result[target] = str(value)
    return result


def _drawio_node_size(node: dict[str, Any]) -> tuple[int, int]:
    node_type = node.get("type")
    if node_type == "decision":
        return 210, 110
    if node_type in {"start", "end"}:
        return 200, 70
    return 210, 80


def _drawio_node_style(node: dict[str, Any]) -> str:
    node_type = node.get("type")
    shape = "rounded=1;arcSize=12;"
    fill = "#F5F5F5"
    stroke = "#666666"
    if node_type in {"start", "end"}:
        shape = "ellipse;perimeter=ellipsePerimeter;"
        fill, stroke = "#D5E8D4", "#82B366"
    elif node_type == "decision":
        shape = "rhombus;perimeter=rhombusPerimeter;"
        fill, stroke = "#FFF2CC", "#D6B656"
    elif node_type == "external":
        shape = "shape=process;backgroundOutline=1;"
        fill, stroke = "#DAE8FC", "#6C8EBF"
    elif node_type == "state":
        fill, stroke = "#E1D5E7", "#9673A6"

    unverified = bool(node.get("assumption_ids"))
    if unverified:
        fill, stroke = "#FFF2CC", "#D79B00"
    dashed = "dashed=1;dashPattern=8 4;" if unverified else ""
    return (
        f"{shape}whiteSpace=wrap;html=1;align=center;verticalAlign=middle;"
        f"fontSize=12;spacing=8;fillColor={fill};strokeColor={stroke};"
        f"strokeWidth=1.5;{dashed}"
    )


def _drawio_edge_style(kind: str) -> str:
    color, dashed = {
        "error": ("#B85450", ""),
        "timeout": ("#D79B00", "dashed=1;dashPattern=8 4;"),
        "cancel": ("#666666", "dashed=1;"),
        "retry": ("#9673A6", "dashed=1;dashPattern=3 3;"),
        "fallback": ("#6C8EBF", "dashed=1;dashPattern=1 3;"),
    }.get(kind, ("#4D4D4D", ""))
    return (
        "edgeStyle=orthogonalEdgeStyle;rounded=0;orthogonalLoop=1;"
        "jettySize=auto;html=1;endArrow=block;endFill=1;"
        f"strokeColor={color};strokeWidth=1.5;{dashed}"
    )


def _drawio_ranks(
    nodes: list[dict[str, Any]], edges: list[dict[str, Any]]
) -> dict[str, int]:
    node_ids = [str(node["id"]) for node in nodes]
    node_id_set = set(node_ids)
    adjacency: dict[str, list[str]] = defaultdict(list)
    for edge in edges:
        source, target = edge.get("from"), edge.get("to")
        if source in node_id_set and target in node_id_set:
            adjacency[str(source)].append(str(target))

    starts = [str(node["id"]) for node in nodes if node.get("type") == "start"]
    if not starts and node_ids:
        starts = [node_ids[0]]
    ranks = {node_id: 0 for node_id in starts}
    queue: deque[str] = deque(starts)
    while queue:
        source = queue.popleft()
        for target in adjacency.get(source, []):
            if target not in ranks:
                ranks[target] = ranks[source] + 1
                queue.append(target)

    fallback_rank = max(ranks.values(), default=-1) + 1
    for node_id in node_ids:
        if node_id not in ranks:
            ranks[node_id] = fallback_rank
            fallback_rank += 1
    return ranks


def _drawio_layout(
    spec: dict[str, Any],
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
) -> tuple[
    dict[str, tuple[int, int, int, int, str]],
    list[tuple[str, str, int, int, int, int, bool]],
    int,
    int,
]:
    direction = str(spec.get("direction", "TD"))
    vertical = direction in {"TD", "TB", "BT"}
    reverse = direction in {"BT", "RL"}
    ranks = _drawio_ranks(nodes, edges)
    max_rank = max(ranks.values(), default=0)

    actors = {
        str(actor.get("id")): str(actor.get("name", actor.get("id")))
        for actor in spec.get("actors", [])
        if isinstance(actor, dict) and actor.get("id")
    }
    used_actors = [
        actor_id
        for actor_id in actors
        if any(node.get("actor_id") == actor_id for node in nodes)
    ]
    has_unassigned = any(node.get("actor_id") not in actors for node in nodes)
    use_lanes = bool(actors) and spec.get("diagram_type", "flowchart") == "flowchart"
    lane_keys = used_actors + (["__unassigned__"] if has_unassigned else [])
    if use_lanes and not lane_keys:
        use_lanes = False

    positions: dict[str, tuple[int, int, int, int, str]] = {}
    lanes: list[tuple[str, str, int, int, int, int, bool]] = []

    def display_rank(node_id: str) -> int:
        rank = ranks[node_id]
        return max_rank - rank if reverse else rank

    if use_lanes and vertical:
        cursor_x = 40
        lane_height = 100 + (max_rank + 1) * 170
        for lane_key in lane_keys:
            lane_nodes = [
                node
                for node in nodes
                if (node.get("actor_id") if node.get("actor_id") in actors else "__unassigned__")
                == lane_key
            ]
            buckets: dict[int, list[dict[str, Any]]] = defaultdict(list)
            for node in lane_nodes:
                buckets[display_rank(str(node["id"]))].append(node)
            max_occupancy = max((len(bucket) for bucket in buckets.values()), default=1)
            lane_width = max(280, 30 + max_occupancy * 230)
            lane_id = drawio_id("lane", lane_key)
            lane_name = actors.get(lane_key, "未分配")
            lanes.append((lane_id, lane_name, cursor_x, 40, lane_width, lane_height, True))
            for rank, bucket in buckets.items():
                slot_width = (lane_width - 30) / len(bucket)
                for slot, node in enumerate(bucket):
                    width, height = _drawio_node_size(node)
                    x = int(15 + (slot + 0.5) * slot_width - width / 2)
                    y = 50 + rank * 170
                    positions[str(node["id"])] = (x, y, width, height, lane_id)
            cursor_x += lane_width + 30
        return positions, lanes, max(850, cursor_x + 10), max(1100, lane_height + 80)

    if use_lanes:
        cursor_y = 40
        lane_width = 100 + (max_rank + 1) * 280
        for lane_key in lane_keys:
            lane_nodes = [
                node
                for node in nodes
                if (node.get("actor_id") if node.get("actor_id") in actors else "__unassigned__")
                == lane_key
            ]
            buckets: dict[int, list[dict[str, Any]]] = defaultdict(list)
            for node in lane_nodes:
                buckets[display_rank(str(node["id"]))].append(node)
            max_occupancy = max((len(bucket) for bucket in buckets.values()), default=1)
            lane_height = max(180, 50 + max_occupancy * 130)
            lane_id = drawio_id("lane", lane_key)
            lane_name = actors.get(lane_key, "未分配")
            lanes.append((lane_id, lane_name, 40, cursor_y, lane_width, lane_height, False))
            for rank, bucket in buckets.items():
                for slot, node in enumerate(bucket):
                    width, height = _drawio_node_size(node)
                    x = 60 + rank * 280
                    y = 35 + slot * 125
                    positions[str(node["id"])] = (x, y, width, height, lane_id)
            cursor_y += lane_height + 30
        return positions, lanes, max(850, lane_width + 80), max(1100, cursor_y + 10)

    buckets: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for node in nodes:
        buckets[display_rank(str(node["id"]))].append(node)
    if vertical:
        max_occupancy = max((len(bucket) for bucket in buckets.values()), default=1)
        for rank, bucket in buckets.items():
            for slot, node in enumerate(bucket):
                width, height = _drawio_node_size(node)
                x = 50 + slot * 250
                y = 50 + rank * 170
                positions[str(node["id"])] = (x, y, width, height, "1")
        return (
            positions,
            lanes,
            max(850, 100 + max_occupancy * 250),
            max(1100, 120 + (max_rank + 1) * 170),
        )

    for rank, bucket in buckets.items():
        for slot, node in enumerate(bucket):
            width, height = _drawio_node_size(node)
            x = 50 + rank * 280
            y = 50 + slot * 130
            positions[str(node["id"])] = (x, y, width, height, "1")
    max_occupancy = max((len(bucket) for bucket in buckets.values()), default=1)
    return (
        positions,
        lanes,
        max(850, 120 + (max_rank + 1) * 280),
        max(1100, 120 + max_occupancy * 130),
    )


def render_drawio(spec: dict[str, Any]) -> str:
    """Render editable, uncompressed native draw.io XML."""
    nodes = [node for node in spec.get("nodes", []) if isinstance(node, dict) and node.get("id")]
    edges = [edge for edge in spec.get("edges", []) if isinstance(edge, dict) and edge.get("id")]
    positions, lanes, page_width, page_height = _drawio_layout(spec, nodes, edges)

    mxfile = ET.Element(
        "mxfile",
        {
            "host": "app.diagrams.net",
            "agent": "FlowSpec",
            "compressed": "false",
            "pages": "1",
        },
    )
    diagram = ET.SubElement(
        mxfile,
        "diagram",
        {"id": "flowspec-page-1", "name": str(spec.get("title", "FlowSpec"))},
    )
    model = ET.SubElement(
        diagram,
        "mxGraphModel",
        {
            "dx": "0",
            "dy": "0",
            "grid": "1",
            "gridSize": "10",
            "guides": "1",
            "tooltips": "1",
            "connect": "1",
            "arrows": "1",
            "fold": "1",
            "page": "1",
            "pageScale": "1",
            "pageWidth": str(page_width),
            "pageHeight": str(page_height),
            "math": "0",
            "shadow": "0",
        },
    )
    graph_root = ET.SubElement(model, "root")
    ET.SubElement(graph_root, "mxCell", {"id": "0"})
    ET.SubElement(graph_root, "mxCell", {"id": "1", "parent": "0"})

    for lane_id, lane_name, x, y, width, height, vertical in lanes:
        lane_style = (
            "swimlane;html=1;rounded=0;collapsible=0;startSize=30;"
            f"horizontal={'1' if vertical else '0'};fillColor=#F8FAFC;"
            "swimlaneFillColor=#FFFFFF;strokeColor=#CBD5E1;fontStyle=1;"
        )
        lane = ET.SubElement(
            graph_root,
            "mxCell",
            {"id": lane_id, "value": _drawio_html(lane_name), "style": lane_style, "vertex": "1", "parent": "1"},
        )
        ET.SubElement(
            lane,
            "mxGeometry",
            {"x": str(x), "y": str(y), "width": str(width), "height": str(height), "as": "geometry"},
        )

    node_ids = {str(node["id"]) for node in nodes}
    for node in nodes:
        node_id = str(node["id"])
        x, y, width, height, parent = positions[node_id]
        attrs = {
            "id": drawio_id("node", node_id),
            "label": _drawio_label(node.get("label", node_id), node_id),
            "tooltip": _drawio_tooltip(node),
            "tags": " ".join(
                item
                for item in (str(node.get("type", "action")), "unverified" if node.get("assumption_ids") else "")
                if item
            ),
            **_drawio_metadata(node, str(node.get("type", "action"))),
        }
        wrapper = ET.SubElement(graph_root, "UserObject", attrs)
        cell = ET.SubElement(
            wrapper,
            "mxCell",
            {"style": _drawio_node_style(node), "vertex": "1", "parent": parent},
        )
        ET.SubElement(
            cell,
            "mxGeometry",
            {"x": str(x), "y": str(y), "width": str(width), "height": str(height), "as": "geometry"},
        )

    state_diagram = spec.get("diagram_type") == "state"
    outgoing: dict[str, list[str]] = defaultdict(list)
    for edge in edges:
        outgoing[str(edge.get("from", ""))].append(str(edge["id"]))
    label_offsets: dict[str, int] = {}
    for edge_ids in outgoing.values():
        for index, edge_id in enumerate(edge_ids):
            label_offsets[edge_id] = int((index - (len(edge_ids) - 1) / 2) * 28)

    for edge in edges:
        edge_id = str(edge["id"])
        label = _edge_label(edge, state=state_diagram)
        attrs = {
            "id": drawio_id("edge", edge_id),
            "label": _drawio_label(label, edge_id),
            "tooltip": _drawio_tooltip(edge),
            "tags": f"edge {edge.get('kind', 'normal')}",
            **_drawio_metadata(edge, f"edge:{edge.get('kind', 'normal')}"),
        }
        wrapper = ET.SubElement(graph_root, "UserObject", attrs)
        cell_attrs = {
            "style": _drawio_edge_style(str(edge.get("kind", "normal"))),
            "edge": "1",
            "parent": "1",
        }
        if edge.get("from") in node_ids:
            cell_attrs["source"] = drawio_id("node", str(edge["from"]))
        if edge.get("to") in node_ids:
            cell_attrs["target"] = drawio_id("node", str(edge["to"]))
        cell = ET.SubElement(wrapper, "mxCell", cell_attrs)
        geometry = ET.SubElement(cell, "mxGeometry", {"relative": "1", "as": "geometry"})
        if label_offsets.get(edge_id):
            ET.SubElement(
                geometry,
                "mxPoint",
                {"x": "0", "y": str(label_offsets[edge_id]), "as": "offset"},
            )

    ET.indent(mxfile, space="  ")
    return ET.tostring(mxfile, encoding="utf-8", xml_declaration=True).decode("utf-8") + "\n"


def _md_cell(value: Any) -> str:
    if isinstance(value, list):
        value = "；".join(str(item) for item in value)
    return str(value or "—").replace("|", "/").replace("\n", " ")


def _bullet_items(items: list[dict[str, Any]], text_field: str) -> list[str]:
    if not items:
        return ["- 无"]
    result = []
    for item in items:
        item_id = item.get("id", "?")
        result.append(f"- **{item_id}**：{item.get(text_field, '—')}")
    return result


def render_markdown(spec: dict[str, Any], issues: list[Issue]) -> str:
    status_labels = {
        "draft": "Draft（草案）",
        "review_ready": "Review-ready（可评审）",
        "verified": "Verified（仅限已有证据的范围）",
    }
    status = str(spec.get("status", "draft"))
    lines = [
        f"# {spec.get('title', 'FlowSpec')}",
        "",
        f"- 状态：{status_labels.get(status, status)}",
        "- 证据边界：结构校验与可重复渲染不等于业务规则真实。",
        "",
    ]
    scope = spec.get("scope", {}) if isinstance(spec.get("scope"), dict) else {}
    lines.extend(
        [
            "## 范围与目标",
            "",
            f"- 目标：{scope.get('goal', '待确认')}",
            f"- 范围内：{_md_cell(scope.get('in_scope', []))}",
            f"- 范围外：{_md_cell(scope.get('out_of_scope', []))}",
            "",
            "## 证据、假设与问题",
            "",
            "### 已确认事实",
            "",
        ]
    )
    facts = spec.get("facts", [])
    if facts:
        for fact in facts:
            lines.append(
                f"- **{fact.get('id', '?')}**：{fact.get('statement', '—')}"
                f"（来源：{_md_cell(fact.get('source_ids', []))}）"
            )
    else:
        lines.append("- 无")

    lines.extend(["", "### 待验证假设", ""])
    assumptions = spec.get("assumptions", [])
    if assumptions:
        for assumption in assumptions:
            lines.extend(
                [
                    f"- **{assumption.get('id', '?')}**：{assumption.get('statement', '—')}",
                    f"  - 风险：{assumption.get('risk', '待补充')}",
                    f"  - 验证：{assumption.get('verification', '待补充')}",
                    f"  - 状态：{assumption.get('status', 'unverified')}",
                ]
            )
    else:
        lines.append("- 无")

    questions = [item for item in spec.get("questions", []) if isinstance(item, dict)]
    blocking = [item for item in questions if item.get("blocking") is True]
    non_blocking = [item for item in questions if item.get("blocking") is not True]
    for heading, items in (("### 阻塞问题", blocking), ("### 非阻塞问题", non_blocking)):
        lines.extend(["", heading, ""])
        if items:
            for item in items:
                lines.append(
                    f"- **{item.get('id', '?')}**：{item.get('question', '—')}"
                    f"（owner：{item.get('owner', '待指定')}）"
                )
        else:
            lines.append("- 无")

    lines.extend(
        [
            "",
            "## draw.io 文件",
            "",
            "- 独立图文件由同一 FlowSpec JSON 执行 `render` 生成；默认扩展名为 `.drawio`。",
            "- Markdown 评审与图文件共享同一份来源、稳定 ID 和追踪关系。",
            "",
            "## 逻辑审计",
            "",
        ]
    )
    if issues:
        for issue in issues:
            refs = f"（{', '.join(issue.refs)}）" if issue.refs else ""
            lines.append(f"- **{issue.severity.upper()} · {issue.code}**：{issue.message}{refs}")
    else:
        lines.append("- 结构校验未发现问题；业务语义仍需人工或真实环境验证。")

    lines.extend(["", "## 验收标准", ""])
    criteria = spec.get("acceptance_criteria", [])
    if criteria:
        for criterion in criteria:
            lines.extend(
                [
                    f"### {criterion.get('id', '?')}",
                    "",
                    f"- 关联节点：{_md_cell(criterion.get('node_ids', []))}",
                    f"- 关联边：{_md_cell(criterion.get('edge_ids', []))}",
                    f"- Given：{criterion.get('given', '—')}",
                    f"- When：{criterion.get('when', '—')}",
                    f"- Then：{_md_cell(criterion.get('then', []))}",
                    f"- 不得发生：{_md_cell(criterion.get('must_not', []))}",
                    f"- 验证证据：{criterion.get('verification', '—')}",
                    f"- 优先级：{criterion.get('priority', 'required')}",
                    "",
                ]
            )
    else:
        lines.extend(["- 无", ""])

    lines.extend(["## 测试场景", ""])
    tests = spec.get("test_scenarios", [])
    if tests:
        for test in tests:
            lines.extend(
                [
                    f"### {test.get('id', '?')} · {test.get('title', '未命名场景')}",
                    "",
                    f"- 类别：{test.get('category', '—')}",
                    f"- 完整路径：{_md_cell(test.get('path', []))}",
                    f"- 覆盖验收：{_md_cell(test.get('covers', []))}",
                    f"- 前置条件：{_md_cell(test.get('preconditions', []))}",
                    f"- 操作步骤：{_md_cell(test.get('steps', []))}",
                    f"- 预期结果：{_md_cell(test.get('expected', []))}",
                    f"- 结束状态：{_md_cell(test.get('postconditions', []))}",
                    f"- 可观测证据：{_md_cell(test.get('observability', []))}",
                    "",
                ]
            )
    else:
        lines.extend(["- 无", ""])

    lines.extend(
        [
            "## 追踪矩阵",
            "",
            "| 需求来源 | 节点 | 边 | 验收标准 | 测试场景 |",
            "| --- | --- | --- | --- | --- |",
        ]
    )
    sources = spec.get("sources", [])
    nodes = spec.get("nodes", [])
    edges = spec.get("edges", [])
    if sources:
        for source in sources:
            source_id = source.get("id")
            source_nodes = [
                node.get("id") for node in nodes if source_id in node.get("source_ids", [])
            ]
            source_edges = [
                edge.get("id") for edge in edges if source_id in edge.get("source_ids", [])
            ]
            source_criteria = [
                criterion.get("id")
                for criterion in criteria
                if set(criterion.get("node_ids", [])).intersection(source_nodes)
                or set(criterion.get("edge_ids", [])).intersection(source_edges)
            ]
            source_tests = [
                test.get("id")
                for test in tests
                if set(test.get("covers", [])).intersection(source_criteria)
            ]
            lines.append(
                f"| {_md_cell(source_id)} | {_md_cell(source_nodes)} | "
                f"{_md_cell(source_edges)} | {_md_cell(source_criteria)} | "
                f"{_md_cell(source_tests)} |"
            )
    else:
        lines.append("| — | — | — | — | — |")
    lines.append("")
    return "\n".join(lines)


def _collection_diff(
    before: dict[str, Any], after: dict[str, Any], key: str
) -> dict[str, Any]:
    def index(spec: dict[str, Any]) -> dict[str, dict[str, Any]]:
        return {
            str(item["id"]): item
            for item in spec.get(key, [])
            if isinstance(item, dict) and item.get("id")
        }

    old = index(before)
    new = index(after)
    changed = []
    for item_id in sorted(set(old).intersection(new)):
        if old[item_id] == new[item_id]:
            continue
        fields = sorted(
            field
            for field in set(old[item_id]).union(new[item_id])
            if old[item_id].get(field) != new[item_id].get(field)
        )
        changed.append({"id": item_id, "changed_fields": fields})
    return {
        "added": sorted(set(new) - set(old)),
        "removed": sorted(set(old) - set(new)),
        "changed": changed,
    }


def diff_specs(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    collections = (
        "sources",
        "facts",
        "assumptions",
        "questions",
        "actors",
        "nodes",
        "edges",
        "acceptance_criteria",
        "test_scenarios",
    )
    changes = {key: _collection_diff(before, after, key) for key in collections}

    def by_id(spec: dict[str, Any], key: str) -> dict[str, dict[str, Any]]:
        return {
            str(item["id"]): item
            for item in spec.get(key, [])
            if isinstance(item, dict) and item.get("id")
        }

    def changed_ids(key: str) -> set[str]:
        change = changes[key]
        result = set(change["added"] + change["removed"])
        result.update(item["id"] for item in change["changed"])
        return result

    changed_source_ids = changed_ids("sources")
    changed_fact_ids = changed_ids("facts")
    changed_assumption_ids = changed_ids("assumptions")
    changed_question_ids = changed_ids("questions")
    changed_actor_ids = changed_ids("actors")
    impacted_nodes = changed_ids("nodes")
    impacted_edges = changed_ids("edges")

    old_nodes = by_id(before, "nodes")
    new_nodes = by_id(after, "nodes")
    old_edges = by_id(before, "edges")
    new_edges = by_id(after, "edges")

    for node_id in set(old_nodes).union(new_nodes):
        for node in (old_nodes.get(node_id), new_nodes.get(node_id)):
            if not node:
                continue
            if (
                node.get("actor_id") in changed_actor_ids
                or set(node.get("source_ids", [])).intersection(changed_source_ids)
                or set(node.get("fact_ids", [])).intersection(changed_fact_ids)
                or set(node.get("assumption_ids", [])).intersection(changed_assumption_ids)
            ):
                impacted_nodes.add(node_id)

    for edge_id in set(old_edges).union(new_edges):
        for edge in (old_edges.get(edge_id), new_edges.get(edge_id)):
            if not edge:
                continue
            if (
                set(edge.get("source_ids", [])).intersection(changed_source_ids)
                or set(edge.get("fact_ids", [])).intersection(changed_fact_ids)
                or set(edge.get("assumption_ids", [])).intersection(changed_assumption_ids)
            ):
                impacted_edges.add(edge_id)

    for edge_id in impacted_edges:
        for edge in (old_edges.get(edge_id), new_edges.get(edge_id)):
            if edge:
                impacted_nodes.update(
                    node_id for node_id in (edge.get("from"), edge.get("to")) if node_id
                )

    old_criteria = by_id(before, "acceptance_criteria")
    new_criteria = by_id(after, "acceptance_criteria")
    impacted_criteria = set(
        changes["acceptance_criteria"]["added"]
        + changes["acceptance_criteria"]["removed"]
    )
    impacted_criteria.update(
        item["id"] for item in changes["acceptance_criteria"]["changed"]
    )
    for criterion_id, criterion in {**old_criteria, **new_criteria}.items():
        if set(criterion.get("node_ids", [])).intersection(impacted_nodes) or set(
            criterion.get("edge_ids", [])
        ).intersection(impacted_edges):
            impacted_criteria.add(criterion_id)

    old_tests = by_id(before, "test_scenarios")
    new_tests = by_id(after, "test_scenarios")
    impacted_tests = set(changes["test_scenarios"]["added"] + changes["test_scenarios"]["removed"])
    impacted_tests.update(item["id"] for item in changes["test_scenarios"]["changed"])
    for test_id, test in {**old_tests, **new_tests}.items():
        if set(test.get("covers", [])).intersection(impacted_criteria) or set(
            test.get("path", [])
        ).intersection(impacted_nodes):
            impacted_tests.add(test_id)

    observability: set[str] = set()
    source_ids = set(changed_source_ids)
    fact_ids = set(changed_fact_ids)
    assumption_ids = set(changed_assumption_ids)
    actor_ids = set(changed_actor_ids)
    for node_id in impacted_nodes:
        for node in (old_nodes.get(node_id), new_nodes.get(node_id)):
            if node:
                observability.update(str(item) for item in node.get("observability", []))
                source_ids.update(str(item) for item in node.get("source_ids", []))
                fact_ids.update(str(item) for item in node.get("fact_ids", []))
                assumption_ids.update(str(item) for item in node.get("assumption_ids", []))
                if node.get("actor_id"):
                    actor_ids.add(str(node["actor_id"]))
    for edge_id in impacted_edges:
        for edge in (old_edges.get(edge_id), new_edges.get(edge_id)):
            if edge:
                source_ids.update(str(item) for item in edge.get("source_ids", []))
                fact_ids.update(str(item) for item in edge.get("fact_ids", []))
                assumption_ids.update(str(item) for item in edge.get("assumption_ids", []))

    return {
        "before": {"title": before.get("title"), "version": before.get("version")},
        "after": {"title": after.get("title"), "version": after.get("version")},
        "changes": changes,
        "impact": {
            "source_ids": sorted(source_ids),
            "fact_ids": sorted(fact_ids),
            "question_ids": sorted(changed_question_ids),
            "actor_ids": sorted(actor_ids),
            "node_ids": sorted(impacted_nodes),
            "edge_ids": sorted(impacted_edges),
            "acceptance_ids": sorted(impacted_criteria),
            "test_ids": sorted(impacted_tests),
            "assumption_ids": sorted(assumption_ids),
            "observability": sorted(observability),
        },
    }


def render_diff_markdown(diff: dict[str, Any]) -> str:
    lines = [
        "# FlowSpec 变更影响",
        "",
        f"- 基线：{diff['before'].get('title', '—')}（{diff['before'].get('version', '—')}）",
        f"- 目标：{diff['after'].get('title', '—')}（{diff['after'].get('version', '—')}）",
        "",
        "## 变更清单",
        "",
        "| 对象 | 新增 | 删除 | 修改 |",
        "| --- | --- | --- | --- |",
    ]
    for key, change in diff["changes"].items():
        changed = [
            f"{item['id']}({', '.join(item['changed_fields'])})" for item in change["changed"]
        ]
        lines.append(
            f"| {key} | {_md_cell(change['added'])} | {_md_cell(change['removed'])} | {_md_cell(changed)} |"
        )
    impact = diff["impact"]
    lines.extend(
        [
            "",
            "## 需同步复核",
            "",
            f"- 需求来源：{_md_cell(impact['source_ids'])}",
            f"- 事实：{_md_cell(impact['fact_ids'])}",
            f"- 待确认问题：{_md_cell(impact['question_ids'])}",
            f"- 参与角色：{_md_cell(impact['actor_ids'])}",
            f"- 节点：{_md_cell(impact['node_ids'])}",
            f"- 边：{_md_cell(impact['edge_ids'])}",
            f"- 验收标准：{_md_cell(impact['acceptance_ids'])}",
            f"- 测试场景：{_md_cell(impact['test_ids'])}",
            f"- 假设：{_md_cell(impact['assumption_ids'])}",
            f"- 日志/埋点：{_md_cell(impact['observability'])}",
            "",
            "> 本报告按稳定 ID 和直接引用计算影响。接口、数据迁移、旧版本兼容及业务语义仍需人工复核。",
            "",
        ]
    )
    return "\n".join(lines)


def _print_issues(issues: list[Issue], json_output: bool = False) -> None:
    if json_output:
        print(json.dumps([asdict(issue) for issue in issues], ensure_ascii=False, indent=2))
        return
    errors = sum(issue.severity == "error" for issue in issues)
    warnings = sum(issue.severity == "warning" for issue in issues)
    for issue in issues:
        print(issue.line())
    print(f"SUMMARY errors={errors} warnings={warnings}")


def _write_or_print(content: str, output: str | None, *, force: bool = False) -> None:
    if output:
        path = Path(output)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not force:
            try:
                with path.open("x", encoding="utf-8") as handle:
                    handle.write(content)
            except FileExistsError as exc:
                raise ValueError(
                    f"Output already exists: {path}; use --force to overwrite it"
                ) from exc
        else:
            temporary_path: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    dir=path.parent,
                    prefix=f".{path.name}.",
                    suffix=".tmp",
                    delete=False,
                ) as handle:
                    handle.write(content)
                    temporary_path = Path(handle.name)
                temporary_path.replace(path)
            finally:
                if temporary_path is not None and temporary_path.exists():
                    temporary_path.unlink()
        print(path)
    else:
        print(content, end="")


def _drawio_output_path(spec: str, output: str | None) -> str:
    path = Path(output) if output else Path(spec).with_suffix(".drawio")
    if path.suffix.lower() != ".drawio":
        raise ValueError("draw.io output path must end with '.drawio'")
    return str(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate_parser = subparsers.add_parser("validate", help="Validate a FlowSpec JSON file")
    validate_parser.add_argument("spec")
    validate_parser.add_argument("--strict", action="store_true", help="Treat warnings as failure")
    validate_parser.add_argument("--json", action="store_true", help="Print findings as JSON")

    render_parser = subparsers.add_parser(
        "render", help="Write a native .drawio file by default, or render a Markdown review"
    )
    render_parser.add_argument("spec")
    render_parser.add_argument(
        "--format", choices=("drawio", "markdown"), default="drawio"
    )
    render_parser.add_argument(
        "--output",
        help="Output path; draw.io output must end with .drawio (defaults beside the input JSON)",
    )
    render_parser.add_argument(
        "--allow-invalid", action="store_true", help="Render even when structural errors exist"
    )
    render_parser.add_argument(
        "--force", action="store_true", help="Replace an existing output file"
    )

    diff_parser = subparsers.add_parser("diff", help="Compare two FlowSpec JSON files")
    diff_parser.add_argument("before")
    diff_parser.add_argument("after")
    diff_parser.add_argument("--format", choices=("json", "markdown"), default="markdown")
    diff_parser.add_argument("--output")
    diff_parser.add_argument(
        "--allow-invalid", action="store_true", help="Compare even when structural errors exist"
    )
    diff_parser.add_argument(
        "--force", action="store_true", help="Replace an existing output file"
    )

    args = parser.parse_args(argv)
    if args.command == "diff":
        try:
            before = load_spec(Path(args.before))
            after = load_spec(Path(args.after))
        except ValueError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        before_issues = validate(before)
        after_issues = validate(after)
        errors = [
            issue
            for issue in before_issues + after_issues
            if issue.severity == "error"
        ]
        if errors and not args.allow_invalid:
            for issue in errors:
                print(issue.line(), file=sys.stderr)
            print("Diff stopped because structural errors exist; use --allow-invalid for a draft.", file=sys.stderr)
            return 1
        result = diff_specs(before, after)
        content = (
            json.dumps(result, ensure_ascii=False, indent=2) + "\n"
            if args.format == "json"
            else render_diff_markdown(result)
        )
        try:
            _write_or_print(content, args.output, force=args.force)
        except (OSError, ValueError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        return 0

    try:
        spec = load_spec(Path(args.spec))
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    issues = validate(spec)
    errors = [issue for issue in issues if issue.severity == "error"]
    warnings = [issue for issue in issues if issue.severity == "warning"]

    if args.command == "validate":
        _print_issues(issues, args.json)
        if errors:
            return 1
        if args.strict and warnings:
            return 1
        return 0

    if errors and not args.allow_invalid:
        for issue in errors:
            print(issue.line(), file=sys.stderr)
        print("Render stopped because structural errors exist; use --allow-invalid for a draft.", file=sys.stderr)
        return 1
    output = args.output
    if args.format == "drawio":
        try:
            output = _drawio_output_path(args.spec, output)
        except ValueError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        content = render_drawio(spec)
    else:
        content = render_markdown(spec, issues)
    try:
        _write_or_print(content, output, force=args.force)
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
