"""Claude adapter accounting correctness (#80 items 2 and 8)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from quota_core.adapters.claude import normalize_claude_quota, scan_claude_local
from quota_core.config import ProviderConfig
from quota_core.snapshot import snapshot_from_dict, snapshot_to_dict, validate_snapshot_dict


def _assistant_line(request_id: str | None, message_id: str, output_tokens: int, **extra: object) -> str:
    record: dict[str, object] = {
        "type": "assistant",
        "uuid": f"uuid-{message_id}-{output_tokens}",
        "timestamp": "2026-10-28T00:00:00Z",
        "message": {
            "id": message_id,
            "model": "claude-opus-5-5",
            "usage": {"input_tokens": 10, "output_tokens": output_tokens, "cache_read_input_tokens": 100},
        },
        **extra,
    }
    if request_id is not None:
        record["requestId"] = request_id
    return json.dumps(record)


class ClaudeLocalScanDedupeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.project = self.root / "proj"
        self.project.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _scan(self):
        return scan_claude_local(ProviderConfig(paths={"projects_dir": str(self.root)}), 1)

    def test_streaming_lines_of_one_request_count_once(self):
        # Claude Code writes one transcript line per content block of a single
        # API response; every line repeats the same requestId and usage.
        lines = [_assistant_line("req_1", "msg_1", 5) for _ in range(3)]
        lines.append(_assistant_line("req_2", "msg_2", 7))
        (self.project / "a.jsonl").write_text("\n".join(lines) + "\n")

        window = self._scan().windows["local_all"]

        self.assertEqual(window.requests, 2)
        self.assertEqual(window.total_tokens, (10 + 5 + 100) + (10 + 7 + 100))
        self.assertEqual(window.by_project["proj"].requests, 2)
        self.assertEqual(window.by_model["claude-opus-5-5"].requests, 2)

    def test_request_repeated_across_transcripts_counts_once(self):
        # A resumed/forked session copies earlier records into a new file.
        (self.project / "a.jsonl").write_text(_assistant_line("req_1", "msg_1", 5) + "\n")
        (self.project / "b.jsonl").write_text(_assistant_line("req_1", "msg_1", 5) + "\n")

        window = self._scan().windows["local_all"]

        self.assertEqual(window.requests, 1)
        self.assertEqual(window.total_tokens, 115)

    def test_streamed_usage_that_grows_counts_the_final_value(self):
        # Later lines of one streamed request carry cumulative output_tokens;
        # the request's cost is its final usage, not the first line's.
        lines = [_assistant_line("req_1", "msg_1", 8), _assistant_line("req_1", "msg_1", 192)]
        (self.project / "a.jsonl").write_text("\n".join(lines) + "\n")

        window = self._scan().windows["local_all"]

        self.assertEqual(window.requests, 1)
        self.assertEqual(window.total_tokens, 10 + 192 + 100)
        self.assertEqual(window.by_project["proj"].total_tokens, 302)
        self.assertEqual(window.by_model["claude-opus-5-5"].total_tokens, 302)

    def test_final_usage_wins_regardless_of_line_order(self):
        # A resumed transcript may hold a later snapshot before an earlier one.
        (self.project / "a.jsonl").write_text(_assistant_line("req_1", "msg_1", 192) + "\n")
        (self.project / "b.jsonl").write_text(_assistant_line("req_1", "msg_1", 8) + "\n")

        window = self._scan().windows["local_all"]

        self.assertEqual(window.requests, 1)
        self.assertEqual(window.total_tokens, 302)

    def test_records_without_request_id_fall_back_to_message_id(self):
        lines = [_assistant_line(None, "msg_1", 5), _assistant_line(None, "msg_1", 5), _assistant_line(None, "msg_2", 5)]
        (self.project / "a.jsonl").write_text("\n".join(lines) + "\n")

        window = self._scan().windows["local_all"]

        self.assertEqual(window.requests, 2)
        self.assertEqual(window.total_tokens, 230)


def _quota_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "fetched_at": 1_000_000,
        "current_session": {"utilization": 0.25, "resets_at": 1_010_000, "tokens_used": 0, "by_project": {}},
    }
    payload.update(overrides)
    return payload


class ClaudeQuotaCacheStateTests(unittest.TestCase):
    def _window(self, payload: dict[str, object], now: int):
        return normalize_claude_quota(payload, now=now).windows["current_session"]

    def test_fresh_payload_without_stale_key_is_live(self):
        window = self._window(_quota_payload(), now=1_000_010)
        self.assertEqual(window.cache_state, "live")
        self.assertFalse(window.stale)

    def test_old_payload_without_stale_key_is_not_live(self):
        self.assertEqual(self._window(_quota_payload(), now=1_000_120).cache_state, "cached")
        old = self._window(_quota_payload(), now=1_000_000 + 3600)
        self.assertEqual(old.cache_state, "stale")
        self.assertTrue(old.stale)

    def test_payload_without_fetched_at_is_unknown_not_live(self):
        payload = _quota_payload()
        del payload["fetched_at"]
        self.assertEqual(self._window(payload, now=1_000_010).cache_state, "unknown")

    def test_explicit_state_and_stale_flag_still_win(self):
        self.assertEqual(self._window(_quota_payload(cache_state="cached"), now=1_000_000 + 3600).cache_state, "cached")
        self.assertEqual(self._window(_quota_payload(stale=True), now=1_000_001).cache_state, "stale")

    def test_missing_utilization_stays_none_through_json_round_trip(self):
        payload = _quota_payload(current_session={"resets_at": 1_010_000, "tokens_used": 0, "by_project": {}})
        snapshot = normalize_claude_quota(payload, now=1_000_010)
        self.assertIsNone(snapshot.windows["current_session"].utilization)

        data = snapshot_to_dict(snapshot)
        self.assertIsNone(data["windows"]["current_session"]["utilization"])
        self.assertEqual(validate_snapshot_dict(data), ())
        self.assertIsNone(snapshot_from_dict(data).windows["current_session"].utilization)

    def test_reported_zero_utilization_stays_zero(self):
        payload = _quota_payload(current_session={"utilization": 0, "resets_at": 1_010_000, "tokens_used": 0})
        self.assertEqual(normalize_claude_quota(payload, now=1_000_010).windows["current_session"].utilization, 0.0)


if __name__ == "__main__":
    unittest.main()
