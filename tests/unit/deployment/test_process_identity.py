"""Preserve persisted runtime fingerprints without Docker or model access."""

import copy
import unittest

from search.operations.deployment import process_identity


ROLES = (
    "scope-model",
    "answer-model",
    "reranker",
    "conversation-api",
    "filtered-embedding",
    "web",
    "web-nginx",
)
# Captured from the three original proof implementations before extraction.
LEGACY_DIGESTS = {
    ROLES[:3]: "7bb7e48e1627db781a60eb7624173198051d9a7b0ce5f2423952fb8c92f222d2",
    ROLES[3:6]: "293c371746ad0bb7164c135210c15df2fc603b2667b7715dd998083dc71a7187",
    ROLES[3:]: "e19611328e50fc70ce1d5c5d99c2e9ef704b5f1a39e751f5e2f1b248888049c9",
    (): "ca3d163bab055381827226140568f3bef7eaac187cebd76878e0b63e9e442356",
}


def rows():
    return {
        role: {
            "State": {
                "Pid": 1000 + index,
                "StartedAt": "2026-10-12T01:02:03.000000000Z",
            }
        }
        for index, role in enumerate(ROLES, 1)
    }


class ProcessIdentityTests(unittest.TestCase):
    def test_persisted_digests_remain_unchanged(self):
        values = rows()
        for roles, expected in LEGACY_DIGESTS.items():
            with self.subTest(roles=roles):
                self.assertEqual(
                    expected,
                    process_identity.start_identity_sha256(
                        {role: values[role] for role in roles}
                    ),
                )

    def test_role_insertion_order_does_not_change_digest(self):
        values = rows()
        for roles, expected in LEGACY_DIGESTS.items():
            with self.subTest(roles=roles):
                self.assertEqual(
                    expected,
                    process_identity.start_identity_sha256(
                        {role: values[role] for role in reversed(roles)}
                    ),
                )

    def test_each_process_start_changes_its_profile_fingerprint(self):
        values = rows()
        for roles, expected in LEGACY_DIGESTS.items():
            for role in roles:
                for field, changed in (
                    ("Pid", values[role]["State"]["Pid"] + 1),
                    ("StartedAt", "2026-10-12T01:02:04.000000000Z"),
                ):
                    with self.subTest(roles=roles, role=role, field=field):
                        current = copy.deepcopy({name: values[name] for name in roles})
                        current[role]["State"][field] = changed
                        self.assertNotEqual(
                            expected, process_identity.start_identity_sha256(current)
                        )


if __name__ == "__main__":
    unittest.main()
