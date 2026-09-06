#!/usr/bin/env python3
"""Test browser: follows the fixture's automatic consent redirect to localhost."""
import sys
from urllib.request import urlopen

with urlopen(sys.argv[1], timeout=30) as response:
    assert response.status == 200
