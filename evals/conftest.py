"""pytest configuration for the Esmi eval suite.

Inserts a brief pause before each test so the 12-test suite stays within
the OpenAI 30K TPM limit on starter/free-tier accounts. Each gpt-4o call
is ~1-2K tokens; 12 tests in ~80s can exhaust the cap at the tail.
"""

import os
import time
import pytest

# Skip the delay in CI (GitHub Actions sets CI=true) — model-gated tests are
# skipped there anyway, so the rate-limit guard is unnecessary and wastes time.
_PRE_TEST_DELAY_S = 0.0 if os.getenv("CI") else 5.0


@pytest.fixture(autouse=True)
def rate_limit_guard():
    time.sleep(_PRE_TEST_DELAY_S)
    yield
