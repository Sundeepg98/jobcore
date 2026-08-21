"""Test isolation from the ambient machine.

Without this, every suite in this repo becomes machine-dependent the moment a
``config/jobhunt.json`` exists anywhere up the tree — which is the single
largest risk in the whole config design, and precisely why ``locate()`` has an
explicit *disabled* mode rather than only a path.

``JOBHUNT_CONFIG`` is set to an explicit disable TOKEN, never to the empty
string: an empty value is what a CI runner or a stray ``env`` block produces by
accident, and "empty means disabled" would make one stray variable silently run
every server on defaults.
"""

import pytest

from jobcore import config as jobcore_config


@pytest.fixture(autouse=True)
def _no_ambient_config(monkeypatch):
    monkeypatch.setenv(jobcore_config.ENV_CONFIG, ":none:")
    monkeypatch.delenv(jobcore_config.ENV_HOME, raising=False)
    monkeypatch.delenv(jobcore_config.ENV_DISABLE, raising=False)
    jobcore_config.invalidate_cache()
    yield
    jobcore_config.invalidate_cache()
