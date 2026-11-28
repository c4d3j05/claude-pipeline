"""Fixtures shared across test modules."""

import os

# macOS only, but harmless elsewhere: `requests` asks the system for proxy settings through Apple's
# frameworks, and a process that has done that and runs threads can crash in the child of a later
# fork, before exec (the "git init ... died with SIGSEGV" and "Fatal Python error: Segmentation
# fault in subprocess._execute_child" failures). No proxy is ever wanted in the offline tests, and
# `no_proxy=*` makes requests skip that lookup entirely.
os.environ.setdefault("no_proxy", "*")
os.environ.setdefault("NO_PROXY", "*")

from .test_gaps import fake_bin  # noqa: F401  (re-exported for tests outside test_gaps)
