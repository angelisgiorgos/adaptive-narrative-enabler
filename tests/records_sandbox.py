"""Keep tests out of the real records database (backend/records.py) and the
repository's artifacts folder.

Imported by every test module that runs simulations, loads engines, plays or
exports: recording and the API's downloads then go to temporary folders for the
whole test run.
"""
import os
import tempfile

os.environ.setdefault("ANE_RECORDS_DIR", tempfile.mkdtemp(prefix="ane-records-tests-"))
os.environ.setdefault("ANE_ARTIFACT_DIR", tempfile.mkdtemp(prefix="ane-artifacts-tests-"))
