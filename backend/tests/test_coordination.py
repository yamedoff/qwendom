from __future__ import annotations

import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from society.schemas.coordination import CoordinationSubtaskHint, TeamCoordinationBrief


class CoordinationSchemaTests(unittest.TestCase):
    def test_brief_round_trips_through_model_dump(self) -> None:
        brief = TeamCoordinationBrief(
            summary="Build a grounded coordination brief",
            proposed_subtasks=[
                CoordinationSubtaskHint(
                    agent_id="builder",
                    subtask="Implement the coordination endpoint",
                    why_assigned="Builder has the strongest delivery focus",
                    done_criteria=["Endpoint returns 200", "Response matches schema"],
                    blocking_if_missing=True,
                ),
                CoordinationSubtaskHint(
                    agent_id="researcher",
                    subtask="Verify Agno Team output_schema behavior",
                    why_assigned="Researcher can gather evidence before implementation",
                    done_criteria=["Confirmed output_schema produces typed response"],
                    blocking_if_missing=False,
                ),
            ],
            open_questions=["Should the brief include risk signals?"],
            recommended_focus="Backend truth first",
            confidence=0.75,
            risk_signals=["output_schema may not be supported by all models"],
            evidence_gaps=["No direct test of Team output_schema in current env"],
            assumptions=["Agno Team supports output_schema in coordinate mode"],
            delegation_hints=["Keep subtasks bounded"],
        )
        dumped = brief.model_dump()
        restored = TeamCoordinationBrief.model_validate(dumped)
        self.assertEqual(restored.summary, brief.summary)
        self.assertEqual(len(restored.proposed_subtasks), 2)
        self.assertEqual(restored.proposed_subtasks[0].agent_id, "builder")
        self.assertTrue(restored.proposed_subtasks[0].blocking_if_missing)
        self.assertEqual(restored.confidence, 0.75)
        self.assertIn("output_schema may not be supported", restored.risk_signals[0])

    def test_brief_defaults_are_safe_for_session_state(self) -> None:
        brief = TeamCoordinationBrief(summary="Minimal brief")
        dumped = brief.model_dump()
        self.assertEqual(dumped["proposed_subtasks"], [])
        self.assertEqual(dumped["open_questions"], [])
        self.assertEqual(dumped["risk_signals"], [])
        self.assertEqual(dumped["evidence_gaps"], [])
        self.assertEqual(dumped["assumptions"], [])
        self.assertEqual(dumped["delegation_hints"], [])
        self.assertEqual(dumped["confidence"], 0.5)
        self.assertEqual(dumped["recommended_focus"], "")

    def test_brief_rejects_out_of_range_confidence(self) -> None:
        with self.assertRaises(Exception):
            TeamCoordinationBrief(summary="Bad", confidence=1.5)
        with self.assertRaises(Exception):
            TeamCoordinationBrief(summary="Bad", confidence=-0.1)

    def test_subtask_hint_carries_delegation_truth_fields(self) -> None:
        hint = CoordinationSubtaskHint(
            agent_id="critic",
            subtask="Review the coordination brief for completeness",
            why_assigned="Critic has the strongest review focus",
            done_criteria=["All truth fields present"],
            blocking_if_missing=True,
        )
        self.assertEqual(hint.agent_id, "critic")
        self.assertTrue(hint.blocking_if_missing)
        self.assertEqual(hint.done_criteria, ["All truth fields present"])


class CoordinationBriefParsingTests(unittest.TestCase):
    def test_parse_coordination_brief_from_json_string(self) -> None:
        from society.orchestrator import SocietyOrchestrator
        import json

        orchestrator = SocietyOrchestrator.__new__(SocietyOrchestrator)
        content = json.dumps({
            "summary": "Parsed from JSON",
            "proposed_subtasks": [
                {"agent_id": "builder", "subtask": "Do the thing", "why_assigned": "Best fit"},
            ],
            "open_questions": ["What about edge cases?"],
            "recommended_focus": "Ship first",
            "confidence": 0.7,
            "risk_signals": ["Edge cases untested"],
            "evidence_gaps": ["No benchmark data"],
            "assumptions": ["Builder can deliver in one pass"],
            "delegation_hints": ["Keep it narrow"],
        })
        brief_dict = orchestrator._parse_coordination_brief(content)
        self.assertEqual(brief_dict["summary"], "Parsed from JSON")
        self.assertEqual(len(brief_dict["proposed_subtasks"]), 1)
        self.assertEqual(brief_dict["risk_signals"], ["Edge cases untested"])
        self.assertEqual(brief_dict["evidence_gaps"], ["No benchmark data"])
        self.assertEqual(brief_dict["assumptions"], ["Builder can deliver in one pass"])

    def test_parse_coordination_brief_falls_back_to_raw_content(self) -> None:
        from society.orchestrator import SocietyOrchestrator

        orchestrator = SocietyOrchestrator.__new__(SocietyOrchestrator)
        brief_dict = orchestrator._parse_coordination_brief("Just raw prose that is not JSON")
        self.assertEqual(brief_dict["summary"], "Just raw prose that is not JSON")
        self.assertEqual(brief_dict["proposed_subtasks"], [])
        self.assertEqual(brief_dict["confidence"], 0.5)
        self.assertEqual(brief_dict["risk_signals"], [])
        self.assertEqual(brief_dict["evidence_gaps"], [])

    def test_parse_coordination_brief_defaults_missing_confidence(self) -> None:
        from society.orchestrator import SocietyOrchestrator
        import json

        orchestrator = SocietyOrchestrator.__new__(SocietyOrchestrator)
        content = json.dumps({
            "summary": "Missing confidence",
            "proposed_subtasks": [],
            "open_questions": [],
            "recommended_focus": "Stay grounded",
        })
        brief_dict = orchestrator._parse_coordination_brief(content)
        self.assertEqual(brief_dict["confidence"], 0.5)

    def test_parse_coordination_brief_coerces_null_confidence_to_default(self) -> None:
        from society.orchestrator import SocietyOrchestrator
        import json

        orchestrator = SocietyOrchestrator.__new__(SocietyOrchestrator)
        content = json.dumps({
            "summary": "Null confidence",
            "proposed_subtasks": [],
            "open_questions": [],
            "recommended_focus": "Stay grounded",
            "confidence": None,
        })
        brief_dict = orchestrator._parse_coordination_brief(content)
        self.assertEqual(brief_dict["confidence"], 0.5)

    def test_build_brief_from_typed_response_content(self) -> None:
        from society.orchestrator import SocietyOrchestrator

        orchestrator = SocietyOrchestrator.__new__(SocietyOrchestrator)
        typed_brief = TeamCoordinationBrief(
            summary="Typed from output_schema",
            proposed_subtasks=[
                CoordinationSubtaskHint(
                    agent_id="builder",
                    subtask="Implement endpoint",
                    why_assigned="Delivery focus",
                    done_criteria=["Returns 200"],
                    blocking_if_missing=True,
                ),
            ],
            open_questions=["Should we add caching?"],
            recommended_focus="Backend truth",
            confidence=0.8,
            risk_signals=["Model may not honor schema"],
            evidence_gaps=["No integration test"],
            assumptions=["output_schema works in coordinate mode"],
        )

        class FakeResponse:
            content = typed_brief

        result = orchestrator._build_coordination_brief_from_response(FakeResponse(), "raw fallback")
        self.assertIsInstance(result, TeamCoordinationBrief)
        self.assertEqual(result.summary, "Typed from output_schema")
        self.assertEqual(len(result.proposed_subtasks), 1)
        self.assertTrue(result.proposed_subtasks[0].blocking_if_missing)
        self.assertEqual(result.confidence, 0.8)

    def test_build_brief_from_dict_response_content(self) -> None:
        from society.orchestrator import SocietyOrchestrator

        orchestrator = SocietyOrchestrator.__new__(SocietyOrchestrator)

        class FakeResponse:
            content = {
                "summary": "Dict content",
                "proposed_subtasks": [],
                "open_questions": ["Dict question"],
                "recommended_focus": "Dict focus",
                "confidence": 0.6,
                "risk_signals": ["Dict risk"],
                "evidence_gaps": [],
                "assumptions": [],
                "delegation_hints": [],
            }

        result = orchestrator._build_coordination_brief_from_response(FakeResponse(), "raw fallback")
        self.assertIsInstance(result, TeamCoordinationBrief)
        self.assertEqual(result.summary, "Dict content")
        self.assertEqual(result.confidence, 0.6)

    def test_build_brief_from_dict_response_with_null_confidence_falls_back_safely(self) -> None:
        from society.orchestrator import SocietyOrchestrator

        orchestrator = SocietyOrchestrator.__new__(SocietyOrchestrator)

        class FakeResponse:
            content = {
                "summary": "Dict content",
                "proposed_subtasks": [],
                "open_questions": [],
                "recommended_focus": "Fallback",
                "confidence": None,
                "risk_signals": [],
                "evidence_gaps": [],
                "assumptions": [],
                "delegation_hints": [],
            }

        result = orchestrator._build_coordination_brief_from_response(FakeResponse(), "raw fallback")
        self.assertIsInstance(result, TeamCoordinationBrief)
        self.assertEqual(result.confidence, 0.5)

    def test_build_brief_falls_back_to_json_parsing(self) -> None:
        from society.orchestrator import SocietyOrchestrator
        import json

        orchestrator = SocietyOrchestrator.__new__(SocietyOrchestrator)
        json_content = json.dumps({
            "summary": "JSON fallback",
            "proposed_subtasks": [],
            "open_questions": [],
            "recommended_focus": "",
            "confidence": 0.5,
            "risk_signals": ["JSON risk"],
            "evidence_gaps": ["JSON gap"],
            "assumptions": [],
            "delegation_hints": [],
        })

        class FakeResponse:
            content = json_content

        result = orchestrator._build_coordination_brief_from_response(FakeResponse(), json_content)
        self.assertIsInstance(result, TeamCoordinationBrief)
        self.assertEqual(result.summary, "JSON fallback")
        self.assertEqual(result.risk_signals, ["JSON risk"])

    def test_build_brief_from_prose_content_uses_safe_defaults(self) -> None:
        from society.orchestrator import SocietyOrchestrator

        orchestrator = SocietyOrchestrator.__new__(SocietyOrchestrator)
        prose_content = "A plain-language coordination note without JSON."

        class FakeResponse:
            content = prose_content

        result = orchestrator._build_coordination_brief_from_response(FakeResponse(), prose_content)
        self.assertIsInstance(result, TeamCoordinationBrief)
        self.assertEqual(result.summary, prose_content)
        self.assertEqual(result.confidence, 0.5)


if __name__ == "__main__":
    unittest.main()
