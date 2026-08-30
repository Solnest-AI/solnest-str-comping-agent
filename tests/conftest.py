"""pytest bootstrap — put the project root on sys.path for `import agent` etc.

Shared fixture-loading helpers live in `tests/_fixtures.py`, not here, so the
test modules import them once by name instead of pytest importing conftest as a
plugin and the tests importing a second copy of it as a module.
"""

import sys
from pathlib import Path

_ROOT = Path(__file__).parent.parent
_TESTS = Path(__file__).parent
for p in (str(_ROOT), str(_TESTS)):
    if p not in sys.path:
        sys.path.insert(0, p)
