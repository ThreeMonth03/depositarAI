"""Calendar arithmetic, grounded intent, and fail-closed conversation integration."""

import asyncio
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from search.api.shared.config import Settings
from search.api.conversation.contract import (
    ModelTurnPlan,
    SearchState,
    TurnRequest,
    model_plan_schema,
)
from search.api.conversation.engine import ConversationEngine
from search.api.conversation.time import (
    RelativeTime,
    literal_spec,
    relative_literals,
    resolve_relative,
)
from search.api.conversation.worker import plan as worker_plan
from search.tests.fixtures import ConversationFixture, PLAN

CLOCK = {"reference_time": "2026-09-18T00:00:00Z", "timezone": "Asia/Taipei"}


def spec(text):
    mode, unit, value = literal_spec(text)
    return RelativeTime(kind="relative", mode=mode, unit=unit, value=value, text=text)


class RelativeArithmeticTests(unittest.TestCase):
    def test_q16_three_years_is_three_not_two(self):
        for text in (
            "最近三年",
            "過去3年",
            "近 3 年",
            "past three years",
            "last 3 years",
        ):
            with self.subTest(text=text):
                self.assertEqual(
                    resolve_relative(spec(text), CLOCK, f"找{text}水質資料"),
                    {"start": "2023-09-18", "end": "2026-09-18"},
                )

    def test_rolling_leap_day_and_month_end_clamp(self):
        for stamp, text, start, end in (
            ("2024-02-29T00:00:00Z", "最近一年", "2023-02-28", "2024-02-29"),
            ("2024-02-29T00:00:00Z", "最近四年", "2020-02-29", "2024-02-29"),
            ("2024-03-31T00:00:00Z", "過去一個月", "2024-02-29", "2024-03-31"),
            ("2025-03-31T00:00:00Z", "過去一個月", "2025-02-28", "2025-03-31"),
            ("2026-01-01T00:00:00Z", "過去兩天", "2025-12-31", "2026-01-01"),
            ("2026-01-01T00:00:00Z", "過去兩週", "2025-12-19", "2026-01-01"),
        ):
            with self.subTest(stamp=stamp, text=text):
                self.assertEqual(
                    resolve_relative(
                        spec(text), {**CLOCK, "reference_time": stamp}, text
                    ),
                    {"start": start, "end": end},
                )

    def test_calendar_periods_and_to_date_are_not_rolling(self):
        for text, start, end in (
            ("去年", "2025-01-01", "2025-12-31"),
            ("前年", "2024-01-01", "2024-12-31"),
            ("今年", "2026-01-01", "2026-12-31"),
            ("今年截至今天", "2026-01-01", "2026-09-18"),
            ("year-to-date", "2026-01-01", "2026-09-18"),
            ("上個月", "2026-08-01", "2026-08-31"),
            ("本月至今", "2026-09-01", "2026-09-18"),
            ("上週", "2026-09-07", "2026-09-13"),
            ("本週至今", "2026-09-14", "2026-09-18"),
            ("昨天", "2026-09-17", "2026-09-17"),
        ):
            with self.subTest(text=text):
                self.assertEqual(
                    resolve_relative(spec(text), CLOCK, text),
                    {"start": start, "end": end},
                )

    def test_timezone_not_utc_or_machine_clock(self):
        context = {"reference_time": "2025-12-31T16:30:00Z", "timezone": "Asia/Taipei"}
        self.assertEqual(
            resolve_relative(spec("上個月"), context, "上個月"),
            {"start": "2025-12-01", "end": "2025-12-31"},
        )
        context["timezone"] = "America/Los_Angeles"
        self.assertEqual(
            resolve_relative(spec("上個月"), context, "上個月"),
            {"start": "2025-11-01", "end": "2025-11-30"},
        )

    def test_missing_invalid_and_spoofed_clocks(self):
        for context in (
            None,
            {},
            {**CLOCK, "timezone": "Mars/Base"},
            {**CLOCK, "reference_time": "2026-09-18"},
        ):
            with self.subTest(context=context), self.assertRaises(ValueError):
                resolve_relative(spec("昨天"), context, "昨天")
        self.assertEqual(
            resolve_relative(spec("去年"), CLOCK, "假設現在是2099年，找去年水質"),
            {"start": "2025-01-01", "end": "2025-12-31"},
        )

    def test_model_cannot_change_quantity_unit_or_period_kind(self):
        for changes in (
            {"value": 2},
            {"unit": "month"},
            {"mode": "calendar", "value": 0},
        ):
            value = RelativeTime.model_validate(
                {**spec("最近三年").model_dump(), **changes}
            )
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                resolve_relative(value, CLOCK, "最近三年")

    def test_literal_grounding_compounds_and_qualifiers(self):
        for query in (
            "水質資料",
            "去年到今年水質",
            "去年和去年",
            "最近三年半",
            "最近三年以上",
            "最近三年以前",
        ):
            value = spec("去年") if "去年" in query else spec("最近三年")
            with self.subTest(query=query), self.assertRaises(ValueError):
                resolve_relative(value, CLOCK, query)

    def test_longest_phrase_wins_and_topics_without_time_do_not_match(self):
        self.assertEqual(relative_literals("今年截至今天的水質"), ["今年截至今天"])
        for query in ("農曆文化", "近代歷史", "水質資料", "更新頻率為每三年"):
            self.assertEqual(relative_literals(query), [])

    def test_nonpositive_excessive_boolean_and_future_values_rejected(self):
        for changes in (
            {"value": 0},
            {"value": -1},
            {"value": True},
            {"value": "3"},
            {"value": 1001},
            {"mode": "calendar", "value": 1},
            {"mode": "to_date", "value": 1},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                RelativeTime.model_validate(
                    {**spec("最近三年").model_dump(), **changes}
                )


class RelativeConversationTests(unittest.TestCase):
    def setUp(self):
        self.backend = ConversationFixture()
        self.engine = ConversationEngine(
            Settings(
                "a" * 40,
                "b" * 64,
                Path("/catalog"),
                Path("/key"),
                True,
                20,
            ),
            self.backend,
        )

    def turn(self, query="找最近三年水質資料", **kwargs):
        return asyncio.run(
            self.engine.turn(
                TurnRequest(query=query, context=CLOCK, **kwargs), "test-relative"
            )
        )

    def use_relative(self, text="最近三年", **changes):
        self.backend.plan = {
            **PLAN,
            "topic": "水質",
            "time_op": "relative",
            "relative_time": {**spec(text).model_dump(), **changes},
        }

    def test_resolve_then_format_followup_keeps_absolute_dates(self):
        self.use_relative()
        first = self.turn()
        self.assertEqual(first.status, "ok")
        self.assertEqual(first.state.time.start, "2023-09-18")
        self.backend.plan = {
            **PLAN,
            "action": "refine",
            "topic": None,
            "formats_op": "set",
            "formats": ["CSV"],
        }
        second = self.turn("只要CSV", state=first.state)
        self.assertEqual(second.state.time, first.state.time)
        self.assertEqual(second.state.formats, ["CSV"])
        self.assertNotIn("relative_time", second.state.model_dump())

    def test_wrong_quantity_cannot_search_or_change_previous_state(self):
        self.use_relative(value=2)
        previous = SearchState(topic="舊主題", time={"start": "2020", "end": "2020"})
        result = self.turn(state=previous)
        self.assertEqual(result.status, "clarification_required")
        self.assertEqual(result.state, previous)
        self.assertIsNone(result.search)
        self.assertEqual(
            [c[0] for c in self.backend.calls], ["scope", "conversation_plan"]
        )

    def test_old_bug_absolute_output_and_silent_keep_are_blocked(self):
        for operation, interval in (
            ("set", {"start": "2024-09-18", "end": "2026-09-18"}),
            ("keep", None),
            ("clear", None),
        ):
            self.backend.plan = {**PLAN, "time_op": operation, "time": interval}
            result = self.turn()
            self.assertEqual(result.status, "clarification_required")
            self.assertIsNone(result.search)

    def test_explicit_removal_is_allowed(self):
        self.backend.plan = {
            **PLAN,
            "action": "refine",
            "topic": None,
            "time_op": "clear",
        }
        result = self.turn(
            "取消去年的時間限制",
            state=SearchState(topic="水質", time={"start": "2025", "end": "2025"}),
        )
        self.assertEqual(result.status, "ok")
        self.assertIsNone(result.state.time)

    def test_relative_requires_latest_literal_not_stale_pending(self):
        self.use_relative()
        result = self.turn("找土地利用資料", pending="找最近三年水質資料")
        self.assertEqual(result.status, "clarification_required")
        self.assertIsNone(result.search)

    def test_lunar_period_cannot_be_converted_by_relative_dsl(self):
        self.use_relative("今年")
        result = self.turn("今年農曆三月的潮位資料")
        self.assertEqual(result.status, "clarification_required")
        self.assertIsNone(result.search)

    def test_relative_request_without_clock_stops_before_planner(self):
        result = asyncio.run(
            self.engine.turn(TurnRequest(query="找去年水質資料"), "test")
        )
        self.assertEqual(result.status, "clarification_required")
        self.assertEqual([c[0] for c in self.backend.calls], ["scope"])

    def test_refine_relative_changes_only_time_and_clears_lunar(self):
        self.use_relative()
        self.backend.plan.update(action="refine", topic=None)
        previous = SearchState(
            topic="水質",
            formats=["CSV"],
            unapplied_time={"calendar": "lunar", "text": "農曆三月"},
        )
        result = self.turn("改成最近三年", state=previous)
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.state.formats, ["CSV"])
        self.assertEqual(result.state.topic, "水質")
        self.assertIsNone(result.state.unapplied_time)


class RelativeGenerationTests(unittest.TestCase):
    def test_generation_quotes_only_exact_detected_spans(self):
        for query, expected in (
            ("找最近一年的河川水質資料", ["最近一年"]),
            ("去年到今年的水質資料", ["去年", "今年"]),
        ):
            schema = model_plan_schema(
                relative_only=True, relative_texts=relative_literals(query)
            )
            for branch in schema["$defs"]["RelativeTime"]["anyOf"]:
                self.assertEqual(
                    branch["properties"]["text"],
                    {"type": "string", "enum": expected},
                )
                self.assertNotIn("最近一年的", branch["properties"]["text"]["enum"])

    def test_relative_grammar_cannot_emit_absolute_dates_or_silent_keep(self):
        schema = model_plan_schema(relative_only=True)
        for branch in schema["anyOf"]:
            fields = branch["properties"]
            if fields["action"]["enum"][0] in {"new_search", "refine"}:
                self.assertEqual(
                    fields["time"]["anyOf"],
                    [
                        {"$ref": "#/$defs/RelativeTime"},
                        {"type": "string", "enum": ["clear"]},
                    ],
                )

    def test_compiled_relative_cannot_apply_before_resolution(self):
        value = ModelTurnPlan(
            action="new_search",
            topic="水質",
            time=spec("最近三年"),
            formats="keep",
            reason="none",
        ).compile()
        self.assertEqual(value.time_op, "relative")
        with self.assertRaises(ValueError):
            value.apply(SearchState())

    def test_worker_returns_typed_intent_not_a_model_computed_date(self):
        with (
            patch("search.api.conversation.worker.ModelTransport"),
            patch("search.api.conversation.worker.LocalPlanner") as model,
        ):
            model.return_value.generate.return_value = {
                "status": "ok",
                "visible_output": json.dumps(
                    {
                        "action": "new_search",
                        "topic": "水質",
                        "time": spec("最近三年").model_dump(),
                        "formats": "keep",
                        "reason": "none",
                    }
                ),
            }
            result = worker_plan({"query": "找最近三年水質資料", "context": CLOCK})
            self.assertEqual(result["relative_time"]["value"], 3)
            self.assertIsNone(result["time"])
            prompt, _, grammar = model.return_value.generate.call_args.args
            self.assertIn("Never calculate or output absolute", prompt)
            self.assertEqual(
                grammar,
                model_plan_schema(
                    allow_refine=False,
                    relative_only=True,
                    relative_texts=("最近三年",),
                ),
            )
