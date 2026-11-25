# A one-machine Slurm cluster with accounting, for the `ssh-slurm` and `local-slurm` stacks.
# Rules: docs/dev/reference/integration-images.md
#
# The controller, the compute node and the accounting daemon with its database all run in this
# one container, started by entrypoint.sh (no systemd). One machine means no shared filesystem to
# set up: a job sees the same files as whoever submitted it, as on a site's shared home.
FROM almalinux:9

# dnf leaves a mirror slower than minrate after `timeout` seconds, instead of finishing on it.
RUN printf '%s\n' 'fastestmirror=True' 'max_parallel_downloads=10' 'minrate=1M' 'timeout=15' \
      >> /etc/dnf/dnf.conf

# renovate: datasource=repology depName=epel_9/slurm
ARG SLURM_VERSION=22.05.11-2.el9

RUN set -eux; \
    dnf install -y epel-release; \
    dnf install -y \
        "slurm-${SLURM_VERSION}" \
        "slurm-slurmctld-${SLURM_VERSION}" \
        "slurm-slurmd-${SLURM_VERSION}" \
        "slurm-slurmdbd-${SLURM_VERSION}" \
        munge mariadb-server openssh-server; \
    dnf clean all; \
    rm -rf /var/cache/dnf

# The `local-slurm` stack runs the suite in this container: Python and what the suite needs, as
# pyproject.toml lists them. The suite itself is the repository, which compose.yml mounts.
RUN set -eux; \
    dnf install -y python3.12; \
    dnf clean all; \
    rm -rf /var/cache/dnf; \
    python3.12 -m venv /opt/suite; \
    /opt/suite/bin/pip install --no-cache-dir \
        asyncssh pydantic pytest pytest-asyncio pytest-xdist pyyaml

# The packages do not create the account the Slurm daemons run as. Jobs run as `dirac`, because
# jobs should not run as root. Its password is "*", not the default "!!": sshd refuses a locked
# account even with a valid key.
RUN set -eux; \
    useradd --system --home-dir /var/lib/slurm --shell /sbin/nologin slurm; \
    install -d -o slurm -g slurm /var/spool/slurm/ctld /var/spool/slurm/d; \
    useradd --create-home dirac; \
    usermod --password '*' dirac

# An empty database: the entrypoint adds Slurm's, with a password made at every start. MariaDB's
# packaged configuration logs to a file; without that line it logs to stderr (integration-stacks.md,
# rule 4).
RUN set -eux; \
    sed -i '/^log-error=/d' /etc/my.cnf.d/mariadb-server.cnf; \
    mariadb-install-db --user=mysql --skip-test-db >/dev/null

# No secret stays in the image: the entrypoint creates these at every start (integration-images.md,
# rule 7).
RUN rm -f /etc/munge/munge.key /etc/ssh/ssh_host_*

# Read before the packaged 50-redhat.conf: for sshd, the first value of a setting wins.
COPY sshd.conf /etc/ssh/sshd_config.d/10-intercede.conf
COPY entrypoint.sh /usr/local/bin/entrypoint.sh

# sshd, for the stacks that reach the cluster as a site's login node.
EXPOSE 22

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
