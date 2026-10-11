import os
import sys
import tempfile

# Headless: never open windows during tests.
os.environ.setdefault('MPLBACKEND', 'Agg')
# Never read the user's private config: point the lookup at an empty directory.
os.environ['XDG_CONFIG_HOME'] = tempfile.mkdtemp(prefix='pystarlight-test-')
os.environ.pop('PYSTARLIGHT_CONFIG', None)
# Let tests import helpers from each other (test_viewer uses test_pipeline's movie maker).
sys.path.insert(0, os.path.dirname(__file__))
