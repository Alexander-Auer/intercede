# The ARC backend

An ARC CE is reached through A-REX's REST interface, over HTTPS with a bearer token. What interCEde
knows about ARC is in `_backends/_arc`: job descriptions, the paths and answers of the interface,
and the state mapping. The backend makes the requests.

## An HTTP client, not `pyarcrest`

ARC's own Python client, `pyarcrest`, would save writing the requests, but it does not fit:

| `pyarcrest` 0.5 | What interCEde needs |
| --- | --- |
| synchronous (`http.client`) | every operation is asynchronous |
| one token per client | a token per operation, with only that operation's scope |
| X.509 proxies, from a file path, which need `cryptography` | tokens only |
| replaces `http.client.print` for the whole process when imported | no change to the consumer's process |

So the backend uses `httpx`. It streams downloads, so the output limits can be counted while a file
arrives, and its `MockTransport` lets the unit tests play an ARC CE without a network.

## ADL, not xRSL

ARC reads both. xRSL is what DIRAC writes, but it is a text format with its own quoting, which is
the kind of quoting the HTCondor submit description needs by hand. ADL is XML: ElementTree escapes
every value, so an argument or a variable arrives as written, whatever it holds. Only what XML
cannot carry at all, such as control characters, is refused.

The descriptions were checked against ARC 7.2's own parser: it reads back every value as written,
and hands the batch system the memory, wall time and slots that were asked for.

## One job per copy, in one request

ARC has no cluster of copies. Each copy gets its own description, with its own environment, and
all of them go to the CE in one request. ARC answers per description, so a copy that is refused
shows up in `Submission.failures` while the others are submitted.

A copy's secrets are not in its description. They are uploaded to the job's session directory after
the job is created, as the executable and local inputs are. URL inputs and outputs are written in
the description, and ARC moves them itself. An upload to a URL is marked mandatory, so a failed
upload fails the job instead of leaving it done without its file.

## States

The mapping follows DIRAC's `AREXComputingElement`, with two differences:

| ARC state | DIRAC | interCEde | Why |
| --- | --- | --- | --- |
| `HELD` | Running, meant to be killed | Waiting | The batch system holds the job and can release it; the same job through the Slurm backend is waiting. Killing it is the consumer's policy. |
| `WIPED` | Aborted | Unknown | ARC removed the job's files, and with them how the job ended. |
