"""An ARC CE, reached through A-REX's REST interface (issue #22).

The backend speaks HTTPS to the CE with a bearer token, as DIRAC's
``AREXComputingElement`` does: each copy is a job of its own, all created in
one request, and the files a job needs from this host are uploaded to its
session directory afterwards. No ARC client tools are needed on this host.

- ``backend``: the contract operations (submit, status)
- ``connection``: the requests, each with the token of its operation
- ``staging``: what each copy's description says, and what is uploaded

What interCEde knows about ARC itself (job descriptions, the paths and answers
of the interface, the state mapping) lives in ``_backends._arc``.
"""

from __future__ import annotations

from intercede._backends.monolith.arc.backend import ARCBackend

__all__ = ["ARCBackend"]
