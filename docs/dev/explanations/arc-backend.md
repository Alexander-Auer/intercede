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
every value, and ARC reads it back exactly. What XML cannot carry at all, such as control
characters, is refused.

The job script ARC writes on the worker is less careful. Tested on ARC 7.2, it passes each argument
through the shell inside double quotes: it escapes quotes and backslashes, but a `$` or a backtick
is expanded, so an argument could run a command. It also drops line breaks, from arguments and
variables. No escaping in the description can prevent that, so such values are refused at submit.
Variables keep a `$` as it is.

Memory, wall time and slots reach the batch system as asked.

## One job per copy, in one request

ARC has no cluster of copies. Each copy gets its own description, with its own environment, and
all of them go to the CE in one request. ARC answers per description, so a copy that is refused
shows up in `Submission.failures` while the others are submitted.

A copy's secrets are not in its description. They are uploaded to the job's session directory after
the job is created, as the executable and local inputs are, and arrive with mode 0600. ARC starts
the job once all of them are there. A job whose files do not all arrive would wait for them until
the CE gives up on it, so the backend cancels it, and the copy is reported as failed.

The session directory is the job's working directory, so the names the backend needs there are
hidden: a local executable is uploaded as `.intercede-executable`, and the output streams default
to `.intercede-stdout` and `.intercede-stderr`. An input or secret with one of these names is
refused. The description does not set `ClientDataPush`: with it, A-REX
waits for a signal from the client that its REST interface has no way to send, and the job never
starts. URL inputs and outputs are written in
the description, and ARC moves them itself. An upload to a URL is marked mandatory, so a failed
upload fails the job instead of leaving it done without its file.

## States

The mapping follows DIRAC's `AREXComputingElement`, with two differences:

| ARC state | DIRAC | interCEde | Why |
| --- | --- | --- | --- |
| `HELD` | Running, meant to be killed | Waiting | The batch system holds the job and can release it; the same job through the Slurm backend is waiting. Killing it is the consumer's policy. |
| `WIPED` | Aborted | Unknown | ARC removed the job's files, and with them how the job ended. |

## Tokens and connections

Every request carries a bearer token with the scopes of its operation, as the WLCG token profile
assigns them: `compute.read` for status, `compute.cancel` to cancel. Submitting is two actions to
ARC, creating the jobs and writing their files, so its token has `compute.create` and
`compute.modify`.

A-REX decides who a connection belongs to at its first request. Tested on ARC 7.2: when that request
is refused, or carries no token, even to a public path, every later request on the same connection
is refused too, whatever its token. So every request carries a token, and after a refusal the
backend drops its connections: the next request opens a new one.

ARC refuses a token with HTTP status 500 and the reason "User can't be assigned configuration", not
with 401. The backend reads that, 401 and 403 as an `AuthenticationError`. Any other failed request
is a `ResourceUnavailableError`.

## Trusting the CE

A CE's host certificate is usually signed by a grid CA, which the system does not know. The backend
trusts the system's CAs, and also those in `X509_CERT_DIR` when it is set, as other grid clients
do. Grid hosts keep them in `/etc/grid-security/certificates`.
