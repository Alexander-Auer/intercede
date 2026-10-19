from __future__ import annotations

from importlib import import_module

from intercede.exceptions import RegistryError

_BACKENDS = {
    "fake": "intercede.testing.fake:FakeBackend",
}
_TRANSPORTS = {
    "ssh": "intercede.transports.ssh:SSHTransport",
}
_SCHEDULERS = {
    "slurm": "intercede.schedulers.slurm:SlurmScheduler",
}

