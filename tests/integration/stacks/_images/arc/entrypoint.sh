#!/bin/bash
# Start a one-machine Slurm cluster and an ARC CE in front of it, in the foreground (no systemd),
# after writing a fresh client token and the CA that signed the CE's certificate to the shared
# credentials folder. Exits as soon as any daemon stops, so `docker compose` and the healthcheck see
# the failure.
set -euo pipefail

CREDENTIALS="${CREDENTIALS_DIR:-/shared/credentials/arc-slurm}"
# The name the harness reaches the CE by, so the one its certificate is for (integration-stacks.md,
# rule 9). arc.conf names the token issuer after it too.
HOST_NAME=localhost

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

# A test CA, and the CE's certificate from it, for both names it is reached by (integration-stacks.md,
# rule 9). Not `arcctl test-ca`: its CA has no keyUsage extension, which Python 3.13 and later refuse.
ca_dir="$(mktemp -d)"
openssl req -x509 -new -newkey rsa:3072 -nodes -days 30 \
    -subj "/O=interCEde test/CN=interCEde test CA" \
    -addext "basicConstraints=critical,CA:true" \
    -addext "keyUsage=critical,keyCertSign,cRLSign" \
    -keyout "$ca_dir/ca.key" -out "$ca_dir/ca.pem" 2>/dev/null
openssl req -new -newkey rsa:3072 -nodes -subj "/O=interCEde test/CN=$HOST_NAME" \
    -keyout /etc/grid-security/hostkey.pem -out "$ca_dir/host.csr" 2>/dev/null
openssl x509 -req -in "$ca_dir/host.csr" -CA "$ca_dir/ca.pem" -CAkey "$ca_dir/ca.key" \
    -CAcreateserial -days 30 -out /etc/grid-security/hostcert.pem -extfile <(printf '%s\n' \
        "subjectAltName=DNS:$HOST_NAME,DNS:$(hostname)" \
        "basicConstraints=critical,CA:false" \
        "keyUsage=critical,digitalSignature,keyEncipherment" \
        "extendedKeyUsage=serverAuth" \
        "authorityKeyIdentifier=keyid,issuer") 2>/dev/null
chmod 0600 /etc/grid-security/hostkey.pem
# The CA is trusted by A-REX like any other, and its key is not needed any more.
install -m 0644 "$ca_dir/ca.pem" /etc/grid-security/certificates/intercede-test-ca.pem
openssl rehash /etc/grid-security/certificates
rm -r "$ca_dir"

# A test token issuer with new signing keys. Deploying it only makes A-REX trust its keys: which
# tokens are let in, and as whom, is arc.conf's business.
arcctl test-jwt init --force
arcctl deploy jwt-issuer "$(arcctl test-jwt export | grep -o 'test-jwt://.*')"

# The client's token and the CA to trust the CE by, readable by everyone: the harness on the host
# copies what needs strict permissions to a private file itself (integration-stacks.md, rule 6).
install -d -m 0755 "$CREDENTIALS"
arcctl test-jwt token --validity 48 \
    --scopes "compute.create compute.read compute.modify compute.cancel" > "$CREDENTIALS/token"
chmod 0644 "$CREDENTIALS/token"
# The CA as a file, and in a folder laid out as X509_CERT_DIR expects, as a grid client trusts it.
install -m 0644 /etc/grid-security/certificates/intercede-test-ca.pem "$CREDENTIALS/ca.pem"
rm -rf "$CREDENTIALS/certificates"
install -d -m 0755 "$CREDENTIALS/certificates"
install -m 0644 "$CREDENTIALS/ca.pem" \
    "$CREDENTIALS/certificates/$(openssl x509 -hash -noout -in "$CREDENTIALS/ca.pem").0"

# -- the cluster ---------------------------------------------------------------------------------

daemon munge runuser -u munge -- munged --foreground
wait_for munge munge -n

install -d -o slurm -g slurm /run/slurm
daemon slurmctld slurmctld -D
wait_for slurmctld scontrol ping

# slurmd's cgroup plugin creates its scope below this directory, which systemd would have made.
mkdir -p /sys/fs/cgroup/system.slice
daemon slurmd slurmd -D

# -- the CE: A-REX, which runs the jobs, and its REST interface ---------------------------------

# The start scripts check arc.conf and write arched's configuration; -f keeps arched in the
# foreground.
daemon arex /usr/share/arc/arc-arex-start -f
daemon arex-ws /usr/share/arc/arc-arex-ws-start -f

status=0
wait -n "${pids[@]}" || status=$?
echo "a daemon stopped (exit $status); stopping the container" >&2
stop
exit "$status"
