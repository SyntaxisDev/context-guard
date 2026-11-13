"""Transport envelopes preserve report delivery without exempting quoted user text."""

import unittest

from core.handoffs import SUBAGENT_RESULT
from tests.handback_cases import REPORTS, USER_PROMPTS


class HandbackEnvelopeTests(unittest.TestCase):
    """Check report framing without exempting quoted or malformed user text."""

    def test_known_transport_prefixes_only(self) -> None:
        for prompt in REPORTS:
            with self.subTest(prompt=prompt):
                self.assertIsNotNone(SUBAGENT_RESULT.match(prompt))
        for prompt in USER_PROMPTS:
            with self.subTest(prompt=prompt):
                self.assertIsNone(SUBAGENT_RESULT.match(prompt))
