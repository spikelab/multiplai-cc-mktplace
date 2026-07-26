"""Tests for revealed-preference verdicts and INTENTION capture in extraction.

**What these prove and what they don't.** Whether the model actually spots
"never do that again" in a transcript is a model behavior, not a code path —
no deterministic test can assert it, and one that mocked the model into
emitting a verdict and then asserted the verdict would be theatre.

So these cover the two halves that *are* deterministic:

  1. the prompt asks for the behavior, with the distinctions that make it
     usable (verdict vs consent, intention vs idle mention);
  2. once the model emits one, it survives the pipeline — parsed, typed, and
     written out with its marker intact rather than silently dropped.

The plan's own measurement for the rest is empirical: after ~3 dream cycles,
check whether changes to `preferences.md` trace back to verdicts.
"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


def _make_mock_client(content: str):
    from multiplai_core.model_client import ModelResponse
    client = AsyncMock()
    client.query = AsyncMock(return_value=ModelResponse(content=content))
    return client


def _unit(learnings: list[dict], diary="Did some work.") -> str:
    body = "".join(
        "<learning>\n" + "".join(f"{k}: {v}\n" for k, v in l.items()) + "</learning>\n"
        for l in learnings)
    return ("<unit>\n"
            "<timestamp>2026-07-26T14:00:00+00:00</timestamp>\n"
            f"<diary>\n{diary}\n</diary>\n"
            f"{body}"
            "</unit>")


class TestVerdictPrompt:
    def test_the_three_verdict_markers_are_specified(self):
        from lib.extraction import EXTRACTION_PROMPT
        for marker in ("verdict: keep", "verdict: kill", "verdict: expand"):
            assert marker in EXTRACTION_PROMPT

    def test_verdicts_are_high_trust_preferences(self):
        """A verdict is the strongest preference signal there is — it's what
        Spike did about a concrete output, not what he said he wanted."""
        from lib.extraction import EXTRACTION_PROMPT
        section = EXTRACTION_PROMPT.split("## Verdict detection")[1]
        assert "PREFERENCE" in section and "verified" in section

    def test_consent_is_explicitly_excluded(self):
        """Without this the extractor turns every "go ahead" into a style
        preference, and preferences.md fills with noise that outvotes the
        real verdicts."""
        from lib.extraction import EXTRACTION_PROMPT
        section = EXTRACTION_PROMPT.split("## Verdict detection")[1]
        assert "go ahead" in section
        assert "not a judgment about output style" in section


class TestIntentionPrompt:
    def test_intention_is_a_listed_type(self):
        from lib.extraction import EXTRACTION_PROMPT
        assert "INTENTION" in EXTRACTION_PROMPT

    def test_both_trigger_shapes_are_specified(self):
        from lib.extraction import EXTRACTION_PROMPT
        section = EXTRACTION_PROMPT.split("## Intention detection")[1]
        assert "due:" in section and "on:" in section

    def test_the_prompt_knows_what_day_it_is(self):
        """"revisit in September" is only resolvable against a known today.
        Without the date the model invents one, and the intention fires in
        the wrong month."""
        from lib.extraction import EXTRACTION_PROMPT
        assert "Today's date:" in EXTRACTION_PROMPT

    def test_todays_date_is_substituted_before_the_call(self, tmp_path):
        import asyncio
        from datetime import datetime, timezone

        from lib.extraction import extract_units

        client = _make_mock_client("<no-units/>")
        asyncio.run(extract_units("some transcript", valid_targets=[], client=client))
        sent = client.query.call_args.kwargs.get("system", "") + \
            str(client.query.call_args.kwargs.get("messages", ""))
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        assert today in sent
        assert "{today}" not in sent


class TestVerdictsSurviveThePipeline:
    def test_a_verdict_learning_round_trips(self):
        import asyncio
        from lib.extraction import extract_units

        response = _unit([{
            "trust": "verified",
            "type": "PREFERENCE",
            "description": "verdict: kill — stop opening replies with a summary of the request",
            "target": "preferences.md",
            "action": "Record that reply preambles are unwanted",
        }])
        units = asyncio.run(extract_units("t", valid_targets=[],
                                          client=_make_mock_client(response)))
        [learning] = units[0]["learnings"]
        assert learning["type"] == "PREFERENCE"
        assert learning["trust"] == "verified"
        assert learning["description"].startswith("verdict: kill")

    def test_the_marker_survives_being_written_to_disk(self, tmp_path):
        """The marker is what makes a verdict findable later; if the writer
        mangles or strips it, the whole experiment is unmeasurable."""
        from lib.extraction import append_learnings

        units = [{
            "timestamp": "2026-07-26T14:00:00+00:00",
            "diary_entry": "x",
            "learnings": [{
                "trust": "verified",
                "type": "PREFERENCE",
                "description": "verdict: expand — more tables like that one",
                "target": "preferences.md",
                "action": "Note the preference for tabular comparisons",
            }],
        }]
        out = tmp_path / "2026-07-26.md"
        append_learnings(units, out, "sess-1", "2026-07-26T14:00:00+00:00")
        text = out.read_text(encoding="utf-8")
        assert "verdict: expand" in text
        assert "PREFERENCE" in text


class TestIntentionsSurviveThePipeline:
    def test_an_intention_learning_round_trips(self):
        import asyncio
        from lib.extraction import extract_units

        response = _unit([{
            "trust": "verified",
            "type": "INTENTION",
            "description": "due: 2026-09-01 — re-check the Italian residency rule",
            "target": "prospective.md",
            "action": "Add the dated intention",
        }])
        units = asyncio.run(extract_units("t", valid_targets=[],
                                          client=_make_mock_client(response)))
        [learning] = units[0]["learnings"]
        assert learning["type"] == "INTENTION"
        assert learning["target"] == "prospective.md"

    def test_an_intention_reaches_the_learnings_file(self, tmp_path):
        from lib.extraction import append_learnings

        units = [{
            "timestamp": "2026-07-26T14:00:00+00:00",
            "diary_entry": "x",
            "learnings": [{
                "trust": "verified",
                "type": "INTENTION",
                "description": "on: the container image moves past v0.5 — re-run the config audit",
                "target": "prospective.md",
                "action": "Add the conditional intention",
            }],
        }]
        out = tmp_path / "2026-07-26.md"
        append_learnings(units, out, "sess-1", "2026-07-26T14:00:00+00:00")
        text = out.read_text(encoding="utf-8")
        assert "INTENTION" in text and "prospective.md" in text

    def test_a_written_intention_is_parseable_by_the_prospective_reader(self):
        """The two ends of the channel must agree on the line format. This is
        the join that silently breaks: extraction emits one shape, the
        SessionStart reader expects another, and nothing ever fires."""
        from datetime import date

        from scripts.lib.prospective import format_line, parse

        line = format_line("re-check the residency rule", due=date(2026, 9, 1),
                           captured=date(2026, 7, 26))
        [i] = parse(line)
        assert i.due == date(2026, 9, 1)
