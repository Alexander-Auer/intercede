# An ARC CE in front of a one-machine Slurm cluster, for the `arc-slurm` stack.
# Rules: docs/dev/reference/integration-images.md
#
# A-REX is the grid gateway interCEde submits to, over its REST interface; it hands each job to the
# Slurm cluster, which runs it. All of it runs in this one container, started by entrypoint.sh (no
# systemd). One machine means no shared filesystem to set up: a job sees its session directory.
FROM almalinux:9

# Shared by every version, so above the ARGs: an ARG is part of the cache key of each later RUN.
# dnf leaves a mirror slower than minrate after `timeout` seconds, instead of finishing on it.
RUN printf '%s\n' 'fastestmirror=True' 'max_parallel_downloads=10' 'minrate=1M' 'timeout=15' \
      >> /etc/dnf/dnf.conf
# ARC needs packages from CRB. The metadata stays cached on purpose: the install below reuses it.
RUN set -eux; \
    dnf install -y epel-release dnf-plugins-core; \
    dnf config-manager --set-enabled crb

# renovate: datasource=repology depName=epel_9/nordugrid-arc7
ARG ARC_VERSION=7.2.0-1.el9
# renovate: datasource=repology depName=epel_9/slurm
ARG SLURM_VERSION=22.05.11-2.el9

RUN set -eux; \
    dnf install -y \
        "nordugrid-arc7-arex-${ARC_VERSION}" \
        "nordugrid-arc7-arcctl-${ARC_VERSION}" \
        "slurm-${SLURM_VERSION}" \
        "slurm-slurmctld-${SLURM_VERSION}" \
        "slurm-slurmd-${SLURM_VERSION}" \
        munge; \
    dnf clean all; \
    rm -rf /var/cache/dnf

# Installing A-REX creates a test CA, a host certificate and the configuration that uses them. No
# secret stays in the image (integration-images.md, rule 7): the entrypoint creates its own at every
# start, and the backend half of the configuration is config/basic/arc.conf.
RUN set -eux; \
    rm -rf /etc/grid-security /etc/arc.conf.d /etc/munge/munge.key; \
    install -d -m 0755 /etc/grid-security/certificates /etc/arc.conf.d

# The packages do not create the account the Slurm daemons run as. Jobs run as `dirac`, the account
# arc.conf maps the client's token to, because jobs should not run as root.
RUN set -eux; \
    useradd --system --home-dir /var/lib/slurm --shell /sbin/nologin slurm; \
    install -d -o slurm -g slurm /var/spool/slurm/ctld /var/spool/slurm/d; \
    useradd --create-home dirac

COPY entrypoint.sh /usr/local/bin/entrypoint.sh

# A-REX's REST interface (config/basic/arc.conf).
EXPOSE 8443

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
