"""Benchmarks that measure what the gateway does for a model, as opposed to tests that check what it does.

Shared pieces live at this level: pinned specs (``corpus``), a connection to a gateway built in-process (``target``),
and the statistics every benchmark reports with (``stats``).
Each benchmark is a subpackage, such as ``search``, and reads its data from a directory outside the package,
so the same code runs the repository's own benchmarks and a user's.
"""
