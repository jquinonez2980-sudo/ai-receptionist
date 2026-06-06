"""pytest configuration for the Esmi eval suite.

Inserts a brief pause before each test so the 12-test suite stays within
the OpenAI 30K TPM limit on starter/free-tier accounts. Each gpt-4o call
is ~1-2K tokens; 12 tests in ~80s can exhaust the cap at the tail.
"""

import time
import pytest

# Seconds to sleep before each test. 5s * 12 tests = 60s added to the run
# but prevents the 429s that previously caused 3 tests to fail at the tail.
_PRE_TEST_DELAY_S = 5.0


@pytest.fixture(autouse=True)
def rate_limit_guard():
    time.sleep(_PRE_TEST_DELAY_S)
    yield
