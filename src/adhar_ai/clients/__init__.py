"""Thin async clients over the real platform APIs.

Nothing in this package fabricates data: every method issues a request and
surfaces what came back (or raises). A tool with no backend configured reports
that it is unconfigured rather than inventing a plausible answer.
"""
