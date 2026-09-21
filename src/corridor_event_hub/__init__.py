"""Corridor Event Hub - corridor roadway event ingest.

Layout mirrors the pipeline stages:

    core/       canonical model, LRS, lifecycle table, confidence scorer
    adapters/   one module per source; the ONLY place state names may appear
    handlers/   Lambda entry points - collect, normalize
    probe.py    run the adapters against live feeds locally, no AWS needed
"""

__version__ = "0.1.0"
