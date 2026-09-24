"""Orchestration: durable jobs with leases, fencing tokens, bounded retries and dead letters.

jobs      job records and their fenced state transitions; PostgreSQL holds the truth
registry  job kinds, work classes and handlers; no recurring schedule is enabled (OD-03)
runner    claims and runs jobs; the worker process drives it alongside inbox acknowledgement
"""
