"""What interCEde knows about ARC, shared by whatever talks to an ARC CE.

Plain functions and data only, with no dependency on any backend class or HTTP
client: the ARC backend makes the requests and uses these to build them and to
read the answers.

- ``description``: a job description in ARC's ADL, one per copy
- ``rest``: the paths and bodies of ARC's REST interface, and parsers for its answers
- ``status``: the state mapping from ARC's job states to ``JobStatus``
"""
