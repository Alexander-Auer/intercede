from __future__ import annotations

_BACKENDS = {
    "fake": "intercede.testing.fake:FakeBackend",
}
_TRANSPORTS = {
    "ssh": "intercede.transports.ssh:SSHTransport",
}
_SCHEDULERS = {
    "slurm": "intercede.schedulers.slurm:SlurmScheduler",
}
