"""Fresh-process import boundaries; no credentials or live calls are needed."""

import subprocess
import sys


def test_behavioral_and_capture_import_do_not_load_sdk_but_public_provider_export_still_works():
    script = """
import sys
from digital_twin.behavioral import Twin
from digital_twin.adapters.mist.behavioral_capture import capture
import digital_twin.providers as providers
assert not any(name == 'mistapi' or name.startswith('mistapi.') for name in sys.modules)
from digital_twin.providers import MistApiProvider
from digital_twin.providers.mist_api import MistApiProvider as implementation
assert MistApiProvider is implementation
try:
    providers.missing_provider
except AttributeError:
    pass
else:
    raise AssertionError('unknown exports must raise AttributeError')
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", script], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
