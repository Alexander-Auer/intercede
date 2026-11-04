#!/bin/bash
# Start a one-machine Slurm cluster in the foreground (no systemd): munge, the accounting database,
# slurmdbd, slurmctld, slurmd and sshd, after writing a fresh SSH key to the shared credentials
# folder. Exits as soon as any daemon stops, so `docker compose` and the healthcheck see the failure.
set -euo pipefail

CREDENTIALS="${CREDENTIALS_DIR:-/shared/credentials/ssh-slurm}"
# How the harness on the host reaches sshd (compose.yml publishes it there).
PUBLISHED_AS="${PUBLISHED_AS:-[127.0.0.1]:2222}"
CLUSTER=intercede
USER_NAME=dirac

# Every daemon runs in the foreground with its log on stdout, under its name, so `docker compose
# logs` holds them all (integration-stacks.md, rule 4).
pids=()
daemon() {
    local tag="$1"
    shift
    "$@" > >(sed -u "s/^/[$tag] /") 2>&1 &
    pids+=("$!")
}

# Only for the order between daemons here: `up --wait` still waits for the healthcheck.
wait_for() {
    local what="$1"
    shift
    for _ in $(seq 1 60); do
        if "$@" >/dev/null 2>&1; then
            return 0
        fi
        sleep 1
    done
    echo "$what did not come up" >&2
    return 1
}

stop() {
    kill "${pids[@]}" 2>/dev/null || true
    wait || true
}
trap 'stop; exit 0' TERM INT

# -- secrets: new at every start, never in the image (integration-images.md, rule 7) ------------

# The key every daemon and client of the cluster shares.
install -d -o munge -g munge /run/munge
dd if=/dev/urandom bs=1 count=1024 of=/etc/munge/munge.key status=none
chown munge:munge /etc/munge/munge.key
chmod 0400 /etc/munge/munge.key

ssh-keygen -q -t ed25519 -N '' -f /etc/ssh/ssh_host_ed25519_key <<<y >/dev/null

# The client's key and the host key to expect, readable by everyone: the harness on the host copies
# what needs strict permissions to a private file itself (integration-stacks.md, rule 6).
install -d -m 0755 "$CREDENTIALS"
rm -f "$CREDENTIALS/id_ed25519" "$CREDENTIALS/id_ed25519.pub"
ssh-keygen -q -t ed25519 -N '' -C "$USER_NAME@intercede-test" -f "$CREDENTIALS/id_ed25519"
chmod 0644 "$CREDENTIALS/id_ed25519"
echo "$PUBLISHED_AS $(cut -d' ' -f1,2 /etc/ssh/ssh_host_ed25519_key.pub)" > "$CREDENTIALS/known_hosts"
chmod 0644 "$CREDENTIALS/known_hosts"

install -d -m 0700 -o "$USER_NAME" -g "$USER_NAME" "/home/$USER_NAME/.ssh"
install -m 0600 -o "$USER_NAME" -g "$USER_NAME" \
    "$CREDENTIALS/id_ed25519.pub" "/home/$USER_NAME/.ssh/authorized_keys"

# -- accounting: what `sacct` reads once the controller has forgotten a job ----------------------

daemon munge runuser -u munge -- munged --foreground
wait_for munge munge -n

install -d -o mysql -g mysql /run/mariadb
daemon mariadb runuser -u mysql -- /usr/libexec/mariadbd --basedir=/usr
wait_for mariadb mariadb-admin ping

password="$(head -c 24 /dev/urandom | base64 | tr -d '/+=')"
mariadb <<SQL
CREATE DATABASE IF NOT EXISTS slurm_acct_db;
CREATE OR REPLACE USER 'slurm'@'localhost' IDENTIFIED BY '$password';
GRANT ALL ON slurm_acct_db.* TO 'slurm'@'localhost';
SQL

# slurmdbd insists on this mode and owner, which is why the file is not a bind mount.
install -m 0600 -o slurm -g slurm /dev/null /etc/slurm/slurmdbd.conf
cat > /etc/slurm/slurmdbd.conf <<CONF
AuthType=auth/munge
DbdHost=localhost
SlurmUser=slurm
StorageType=accounting_storage/mysql
StorageHost=localhost
StorageUser=slurm
StoragePass=$password
StorageLoc=slurm_acct_db
CONF

install -d -o slurm -g slurm /run/slurm
daemon slurmdbd slurmdbd -D
wait_for slurmdbd sacctmgr --noheader list cluster

# `|| true`: on a restart of the same container they exist already.
sacctmgr --immediate add cluster "$CLUSTER" >/dev/null || true
sacctmgr --immediate add account "$USER_NAME" cluster="$CLUSTER" >/dev/null || true
sacctmgr --immediate add user "$USER_NAME" account="$USER_NAME" >/dev/null || true

# -- the cluster ---------------------------------------------------------------------------------

daemon slurmctld slurmctld -D
wait_for slurmctld scontrol ping

# slurmd's cgroup plugin creates its scope below this directory, which systemd would have made.
mkdir -p /sys/fs/cgroup/system.slice
daemon slurmd slurmd -D

daemon sshd /usr/sbin/sshd -D -e

status=0
wait -n "${pids[@]}" || status=$?
echo "a daemon stopped (exit $status); stopping the container" >&2
stop
exit "$status"
