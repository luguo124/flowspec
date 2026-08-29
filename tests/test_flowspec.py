from __future__ import annotations

import copy
import io
import importlib.util
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "flowspec.py"
SPEC = importlib.util.spec_from_file_location("flowspec_module", MODULE_PATH)
assert SPEC and SPEC.loader
flowspec = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = flowspec
SPEC.loader.exec_module(flowspec)


class FlowSpecTests(unittest.TestCase):
    def setUp(self) -> None:
        self.example = flowspec.load_spec(
            ROOT / "references" / "login-security.example.json"
        )

    def codes(self, payload: dict) -> list[str]:
        return [issue.code for issue in flowspec.validate(payload)]

    def test_example_has_no_structural_errors(self) -> None:
        issues = flowspec.validate(self.example)
        self.assertFalse([issue for issue in issues if issue.severity == "error"])
        self.assertEqual(
            [issue.code for issue in issues], ["blocking_question_open", "blocking_question_open"]
        )

    def test_review_ready_rejects_open_blocking_questions(self) -> None:
        payload = copy.deepcopy(self.example)
        payload["status"] = "review_ready"
        issues = flowspec.validate(payload)
        blockers = [issue for issue in issues if issue.code == "blocking_question_open"]
        self.assertEqual(len(blockers), 2)
        self.assertTrue(all(issue.severity == "error" for issue in blockers))

    def test_verified_rejects_unverified_assumptions(self) -> None:
        payload = copy.deepcopy(self.example)
        payload["status"] = "verified"
        payload["questions"] = [
            question for question in payload["questions"] if not question["blocking"]
        ]
        self.assertIn("unverified_assumption_in_verified_spec", self.codes(payload))

    def test_unknown_edge_source_reference_is_error(self) -> None:
        payload = copy.deepcopy(self.example)
        payload["edges"][0]["source_ids"] = ["SRC-MISSING"]
        issues = flowspec.validate(payload)
        self.assertTrue(
            any(
                issue.code == "unknown_reference"
                and issue.refs == ("E-001",)
                and issue.severity == "error"
                for issue in issues
            )
        )

    def test_decision_branch_requires_direct_acceptance_trace(self) -> None:
        payload = copy.deepcopy(self.example)
        for criterion in payload["acceptance_criteria"]:
            criterion["edge_ids"] = [
                edge_id for edge_id in criterion["edge_ids"] if edge_id != "E-004"
            ]
        self.assertIn("branch_without_acceptance", self.codes(payload))

    def test_invalid_test_path_is_error(self) -> None:
        payload = copy.deepcopy(self.example)
        payload["test_scenarios"][0]["path"] = [
            "N-START",
            "N-CHECK",
            "N-END-OK",
        ]
        self.assertIn("invalid_test_path", self.codes(payload))

    def test_markdown_separates_questions_and_renders_edge_trace(self) -> None:
        rendered = flowspec.render_markdown(
            self.example, flowspec.validate(self.example)
        )
        self.assertIn("### 阻塞问题", rendered)
        self.assertIn("### 非阻塞问题", rendered)
        self.assertIn("关联边：E-001", rendered)
        self.assertIn("| 需求来源 | 节点 | 边 | 验收标准 | 测试场景 |", rendered)
        self.assertIn("E-015", rendered)
        self.assertIn("## draw.io 文件", rendered)
        self.assertNotIn("```mermaid", rendered)

    def test_drawio_ids_do_not_inherit_mermaid_collisions(self) -> None:
        payload = {
            "version": "1.0",
            "title": "ID 兼容",
            "diagram_type": "flowchart",
            "status": "draft",
            "nodes": [
                {"id": "N-A", "type": "start", "label": "开始"},
                {"id": "N_A", "type": "action", "label": "处理"},
                {"id": "N-END", "type": "end", "label": "结束"},
            ],
            "edges": [
                {"id": "E-1", "from": "N-A", "to": "N_A"},
                {"id": "E-2", "from": "N_A", "to": "N-END"},
            ],
            "acceptance_criteria": [
                {
                    "id": "AC-1",
                    "node_ids": ["N_A"],
                    "edge_ids": [],
                    "given": "流程已开始",
                    "when": "执行处理",
                    "then": ["流程结束"],
                }
            ],
            "test_scenarios": [
                {
                    "id": "TC-1",
                    "category": "happy",
                    "covers": ["AC-1"],
                    "path": ["N-A", "N_A", "N-END"],
                    "preconditions": ["流程可用"],
                    "steps": ["执行处理"],
                    "expected": ["流程结束"],
                }
            ],
        }
        errors = [issue for issue in flowspec.validate(payload) if issue.severity == "error"]
        self.assertFalse(errors)
        root = ET.fromstring(flowspec.render_drawio(payload))
        self.assertIsNotNone(root.find(".//UserObject[@id='node-N-A']"))
        self.assertIsNotNone(root.find(".//UserObject[@id='node-N_A']"))

    def test_drawio_render_is_editable_native_xml_with_trace_metadata(self) -> None:
        rendered = flowspec.render_drawio(self.example)
        root = ET.fromstring(rendered)
        self.assertEqual(root.tag, "mxfile")
        self.assertEqual(root.get("compressed"), "false")
        self.assertEqual(len(root.findall("diagram")), 1)

        graph_root = root.find("./diagram/mxGraphModel/root")
        self.assertIsNotNone(graph_root)
        assert graph_root is not None
        self.assertIsNotNone(graph_root.find("./mxCell[@id='0']"))
        self.assertIsNotNone(graph_root.find("./mxCell[@id='1'][@parent='0']"))

        objects = graph_root.findall("UserObject")
        object_ids = [item.get("id") for item in objects]
        self.assertEqual(len(object_ids), len(set(object_ids)))
        node_ids = {
            item.get("id")
            for item in objects
            if item.get("flowspecType") in flowspec.NODE_TYPES
        }
        edge_objects = [item for item in objects if item.get("flowspecType", "").startswith("edge:")]
        self.assertEqual(len(node_ids), len(self.example["nodes"]))
        self.assertEqual(len(edge_objects), len(self.example["edges"]))

        for item in objects:
            cell = item.find("mxCell")
            self.assertIsNotNone(cell)
            assert cell is not None
            self.assertIsNotNone(cell.find("mxGeometry"))
            if cell.get("edge") == "1":
                self.assertIn(cell.get("source"), node_ids)
                self.assertIn(cell.get("target"), node_ids)
                self.assertEqual(cell.find("mxGeometry").get("relative"), "1")

        challenge = graph_root.find("./UserObject[@id='node-N-CHALLENGE']")
        self.assertIsNotNone(challenge)
        assert challenge is not None
        self.assertEqual(challenge.get("sourceIds"), "SRC-001")
        self.assertEqual(challenge.get("assumptionIds"), "H-003")
        self.assertIn("N-CHALLENGE", challenge.get("label", ""))

        challenge_edge = graph_root.find("./UserObject[@id='edge-E-008']/mxCell/mxGeometry/mxPoint")
        self.assertIsNotNone(challenge_edge)

    def test_drawio_render_escapes_user_html(self) -> None:
        payload = copy.deepcopy(self.example)
        payload["nodes"][0]["label"] = "<script>alert('x')</script> & 开始"
        root = ET.fromstring(flowspec.render_drawio(payload))
        item = root.find(".//UserObject[@id='node-N-START']")
        self.assertIsNotNone(item)
        assert item is not None
        label = item.get("label", "")
        self.assertNotIn("<script>", label)
        self.assertIn("&lt;script&gt;", label)

    def test_drawio_cli_writes_drawio_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "login.drawio"
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                result = flowspec.main(
                    [
                        "render",
                        str(ROOT / "references" / "login-security.example.json"),
                        "--format",
                        "drawio",
                        "--output",
                        str(output),
                    ]
                )
            self.assertEqual(result, 0)
            self.assertEqual(ET.parse(output).getroot().tag, "mxfile")
            self.assertIn(str(output), stdout.getvalue())

    def test_drawio_cli_preserves_existing_output_without_force(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "login.drawio"
            output.write_text("keep my edits", encoding="utf-8")
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                result = flowspec.main(
                    [
                        "render",
                        str(ROOT / "references" / "login-security.example.json"),
                        "--output",
                        str(output),
                    ]
                )
            self.assertEqual(result, 2)
            self.assertEqual(output.read_text(encoding="utf-8"), "keep my edits")
            self.assertIn("use --force", stderr.getvalue())

    def test_drawio_cli_force_replaces_existing_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "login.drawio"
            output.write_text("stale output", encoding="utf-8")
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                result = flowspec.main(
                    [
                        "render",
                        str(ROOT / "references" / "login-security.example.json"),
                        "--output",
                        str(output),
                        "--force",
                    ]
                )
            self.assertEqual(result, 0)
            self.assertEqual(ET.parse(output).getroot().tag, "mxfile")
            self.assertIn(str(output), stdout.getvalue())

    def test_drawio_is_default_and_writes_beside_input(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = ROOT / "references" / "login-security.example.json"
            spec_path = Path(directory) / "login.json"
            spec_path.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
            output = spec_path.with_suffix(".drawio")
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                result = flowspec.main(["render", str(spec_path)])
            self.assertEqual(result, 0)
            self.assertTrue(output.is_file())
            self.assertEqual(ET.parse(output).getroot().tag, "mxfile")
            self.assertFalse(spec_path.with_suffix(".mmd").exists())
            self.assertIn(str(output), stdout.getvalue())

    def test_mermaid_is_not_a_cli_output_format(self) -> None:
        stderr = io.StringIO()
        with redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
            flowspec.main(
                [
                    "render",
                    str(ROOT / "references" / "login-security.example.json"),
                    "--format",
                    "mermaid",
                ]
            )
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("invalid choice", stderr.getvalue())

    def test_drawio_cli_rejects_non_drawio_extension(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "login.mmd"
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                result = flowspec.main(
                    [
                        "render",
                        str(ROOT / "references" / "login-security.example.json"),
                        "--output",
                        str(output),
                    ]
                )
            self.assertEqual(result, 2)
            self.assertFalse(output.exists())
            self.assertIn("must end with '.drawio'", stderr.getvalue())

    def test_drawio_state_edge_preserves_event_guard_and_effect(self) -> None:
        payload = {
            "version": "1.0",
            "title": "订单状态",
            "diagram_type": "state",
            "direction": "LR",
            "nodes": [
                {"id": "N-START", "type": "start", "label": "开始"},
                {"id": "N-A", "type": "state", "label": "待支付"},
                {"id": "N-B", "type": "state", "label": "已支付"},
                {"id": "N-END", "type": "end", "label": "结束"},
            ],
            "edges": [
                {"id": "E-1", "from": "N-START", "to": "N-A"},
                {
                    "id": "E-2",
                    "from": "N-A",
                    "to": "N-B",
                    "event": "支付成功",
                    "guard": "金额一致",
                    "effect": "记录支付时间",
                },
                {"id": "E-3", "from": "N-B", "to": "N-END"},
            ],
        }
        root = ET.fromstring(flowspec.render_drawio(payload))
        edge = root.find(".//UserObject[@id='edge-E-2']")
        self.assertIsNotNone(edge)
        assert edge is not None
        label = edge.get("label", "")
        self.assertIn("支付成功 [金额一致] / 记录支付时间", label)

    def test_source_change_propagates_to_edges_criteria_and_tests(self) -> None:
        after = copy.deepcopy(self.example)
        after["sources"][0]["ref"] += "（更新）"
        result = flowspec.diff_specs(self.example, after)
        impact = result["impact"]
        self.assertIn("SRC-001", impact["source_ids"])
        self.assertIn("E-001", impact["edge_ids"])
        self.assertIn("AC-001", impact["acceptance_ids"])
        self.assertIn("TC-001", impact["test_ids"])

    def test_state_transition_without_event_is_warning(self) -> None:
        payload = {
            "version": "1.0",
            "title": "状态示例",
            "diagram_type": "state",
            "status": "draft",
            "nodes": [
                {"id": "N-START", "type": "start", "label": "开始"},
                {"id": "N-A", "type": "state", "label": "A", "acceptance_ids": ["AC-1"]},
                {"id": "N-B", "type": "state", "label": "B", "acceptance_ids": ["AC-1"]},
                {"id": "N-END", "type": "end", "label": "结束"}
            ],
            "edges": [
                {"id": "E-1", "from": "N-START", "to": "N-A"},
                {"id": "E-2", "from": "N-A", "to": "N-B"},
                {"id": "E-3", "from": "N-B", "to": "N-END"}
            ],
            "acceptance_criteria": [
                {
                    "id": "AC-1",
                    "node_ids": ["N-A", "N-B"],
                    "given": "实体处于 A",
                    "when": "发生转换",
                    "then": ["实体进入 B"]
                }
            ]
        }
        self.assertIn("state_transition_without_event", self.codes(payload))


if __name__ == "__main__":
    unittest.main()
