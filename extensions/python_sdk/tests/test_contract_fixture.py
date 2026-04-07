from __future__ import annotations

import json
import unittest
from pathlib import Path


class ContractFixtureTests(unittest.TestCase):
    def test_hub_session_contract_has_expected_groups(self):
        fixture_path = Path(__file__).resolve().parents[3] / "contracts" / "hub_session_protocol_v1.json"
        payload = json.loads(fixture_path.read_text())

        self.assertEqual("hub_session_protocol_v1", payload["name"])
        self.assertEqual(1, payload["version"])

        groups = payload["message_groups"]
        self.assertEqual(
            {"cli_to_hub", "hub_to_cli", "client_to_hub", "hub_to_client"},
            set(groups.keys()),
        )

        for name, values in groups.items():
            self.assertTrue(values, f"{name} should not be empty")
            self.assertEqual(len(values), len(set(values)), f"{name} should not contain duplicates")

        self.assertIn("create_session", groups["cli_to_hub"])
        self.assertIn("subscribe", groups["client_to_hub"])
        self.assertIn("subscribed", groups["hub_to_client"])
        self.assertIn("resync_request", groups["hub_to_cli"])

    def test_contract_invariants_remain_explicit(self):
        fixture_path = Path(__file__).resolve().parents[3] / "contracts" / "hub_session_protocol_v1.json"
        payload = json.loads(fixture_path.read_text())

        invariants = payload["invariants"]
        self.assertGreaterEqual(len(invariants), 4)
        self.assertIn("input remains shared regardless of controller", invariants)
        self.assertIn("resize is controller-gated", invariants)


if __name__ == "__main__":
    unittest.main()
