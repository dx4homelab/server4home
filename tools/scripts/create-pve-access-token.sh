#!/usr/bin/env bash
#
# create-pve-access-token.sh — provision a Proxmox VE API token for server4home.
#
# Runs **on the Proxmox host**, as root (uses `pveum`, the local PVE CLI).
# Creates a dedicated PVE-realm user, generates an API token, grants it
# Administrator at /, and validates the token works against the local API
# before printing a secrets.yaml-ready block.
#
# Idempotent: re-running the script after a successful run is safe.
# Token-recreation (`--rotate`) is opt-in because rotating invalidates
# any existing kubeconfigs / runner instances using the old secret.
#
# ─── Why every step is where it is ────────────────────────────────────────
# The history this script encodes:
#
#  1. `realm=pve` (NOT pam). PAM-realm users have to be existing Linux
#     accounts; we just want a PVE-internal identity that exists only to
#     hold the token. The `pve` realm is built for this.
#
#  2. Token **privilege separation = 1** (PVE's default). Means the token
#     carries its own ACL, independent of the user's. That's what we want
#     for an automation credential: scope is on the token, not the user.
#     The corollary, and the foot-gun we hit before: ACL must be granted
#     with `-tokens <name>`, NOT just on the user. The script does both
#     because doubling up is harmless and protects against someone later
#     flipping privsep off and wondering why the token still works.
#
#  3. Role = Administrator at `/`. Yes, that's broad. We tried minimum-
#     permission setups and discovered that the PVE `args` config field
#     ("only root over local CLI can set this") is enforced regardless of
#     role — so no API-token grant unlocks it. The runner sidesteps that
#     by SSHing in as root for `qm set --args`. Administrator is the
#     simplest role that makes the rest of the API surface work.
#
#  4. The token is *separate from web UI access*. Tokens can't be used
#     to log into the PVE web UI — that needs a user+password. Keep your
#     human-login admin user (`developer@pam` or similar) untouched; this
#     script doesn't change that.
#
# ─── Usage ────────────────────────────────────────────────────────────────
#   scp tools/scripts/create-pve-access-token.sh root@pve:/tmp/
#   ssh root@pve bash /tmp/create-pve-access-token.sh
#
# Options (all have sane defaults):
#   --user <name>          PVE-realm user (default: server4home-bot)
#   --token <name>         token id (default: deploy)
#   --role <role>          role granted at / (default: Administrator)
#   --rotate               delete + recreate the token if it already exists
#                          (default: keep the existing token, just re-verify)
#   --write-secret <path>  also write the credential to this file in
#                          secrets.yaml format (default: print to stdout only)
#   --help, -h             show this help

set -euo pipefail

# ─── Defaults ─────────────────────────────────────────────────────────────
PVE_USER="server4home"
REALM="pve"
TOKEN_NAME="deploy"
ROLE="Administrator"
ROTATE=0
WRITE_SECRET_TO=""

usage() { sed -n '2,/^set -euo/p' "$0" | sed 's/^# \{0,1\}//' | head -n -2; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --user)         PVE_USER="$2"; shift 2 ;;
        --token)        TOKEN_NAME="$2"; shift 2 ;;
        --role)         ROLE="$2"; shift 2 ;;
        --rotate)       ROTATE=1; shift ;;
        --write-secret) WRITE_SECRET_TO="$2"; shift 2 ;;
        -h|--help)      usage; exit 0 ;;
        *) echo "unknown arg: $1" >&2; usage >&2; exit 2 ;;
    esac
done

FULL_TOKEN_ID="${PVE_USER}@${REALM}!${TOKEN_NAME}"
TOKEN_SECRET=""   # filled in by create_api_token() or kept empty on no-rotate

log()  { printf '[create-access-token] %s\n' "$*"; }
die()  { printf '[create-access-token] ERROR: %s\n' "$*" >&2; exit 1; }

# ─── 0. Sanity checks ─────────────────────────────────────────────────────
sanity_checks() {
    [[ $EUID -eq 0 ]] || die "must run as root on the PVE host (uses pveum)"
    command -v pveum >/dev/null || die "pveum not found — is this a PVE host?"
    command -v curl  >/dev/null || die "curl not found"
    command -v jq    >/dev/null || die "jq not found (apt-get install -y jq)"
}

# ─── 1. Create the PVE-realm user (idempotent) ────────────────────────────
ensure_user() {
    if pveum user list --output-format json | jq -e \
            ".[] | select(.userid==\"${PVE_USER}@${REALM}\")" >/dev/null; then
        log "user ${PVE_USER}@${REALM}: already exists"
    else
        log "creating user ${PVE_USER}@${REALM}"
        pveum user add "${PVE_USER}@${REALM}" \
            --comment "server4home runner automation; do not delete"
    fi
}

# ─── 2. Create (or rotate) the API token ──────────────────────────────────
# Token privilege separation = 1 (default) is what we want — the ACL we
# grant in step 3 is bound to the TOKEN, not inherited from the user.
ensure_token() {
    local exists=0
    if pveum user token list "${PVE_USER}@${REALM}" --output-format json \
            | jq -e ".[] | select(.tokenid==\"${TOKEN_NAME}\")" >/dev/null; then
        exists=1
    fi

    if (( exists )) && (( ! ROTATE )); then
        log "token ${FULL_TOKEN_ID}: already exists (use --rotate to replace)"
        return 0
    fi

    if (( exists )) && (( ROTATE )); then
        log "rotating token ${FULL_TOKEN_ID} — deleting existing"
        pveum user token remove "${PVE_USER}@${REALM}" "${TOKEN_NAME}"
    fi

    log "creating token ${FULL_TOKEN_ID} (privsep=1)"
    local token_json
    token_json=$(pveum user token add "${PVE_USER}@${REALM}" "${TOKEN_NAME}" \
                    --privsep 1 \
                    --output-format json)
    TOKEN_SECRET=$(echo "$token_json" | jq -r '.value')
    [[ -n "$TOKEN_SECRET" && "$TOKEN_SECRET" != "null" ]] \
        || die "could not extract token secret from pveum output"
}

# ─── 3. Grant ACL to the token (and to the user, defensively) ────────────
# The `-tokens` flag is the load-bearing one with privsep=1: it grants the
# permission to THIS token specifically. We also grant on the user level
# so the credential keeps working if someone later flips privsep off.
ensure_acl() {
    log "granting ${ROLE} at / to token ${FULL_TOKEN_ID}"
    # `pveum acl modify --tokens` wants the FULL token id (`user@realm!name`),
    # not just the token name — same format the auth header uses. Passing only
    # the short name dies with: "tokens: invalid format - value 'X' does not
    # look like a valid token ID".
    pveum acl modify / \
        --roles "${ROLE}" \
        --users "${PVE_USER}@${REALM}" \
        --tokens "${FULL_TOKEN_ID}"

    log "also granting ${ROLE} at / to user ${PVE_USER}@${REALM} (privsep insurance)"
    pveum acl modify / \
        --roles "${ROLE}" \
        --users "${PVE_USER}@${REALM}"
}

# ─── 4. Validate — two distinct calls catch different misconfigs ─────────
# Read-class call: /access/permissions confirms the auth header parses and
# the token has SOME ACL. Empty permissions means privsep ACL is missing.
# Write-class lookup: /cluster/nextid is read-only but exercises a code
# path that several misconfigurations fail differently from /permissions.
validate() {
    if [[ -z "$TOKEN_SECRET" ]]; then
        log "no fresh secret to validate (token was kept, not rotated)"
        log "skipping validation — re-run with --rotate if you need to re-verify"
        return 0
    fi

    local auth="Authorization: PVEAPIToken=${FULL_TOKEN_ID}=${TOKEN_SECRET}"
    local base="https://127.0.0.1:8006/api2/json"
    local body status

    log "[validation 1/2] GET /access/permissions"
    body=$(curl --silent --insecure --max-time 10 \
        --write-out '\n__STATUS:%{http_code}' \
        -H "$auth" "${base}/access/permissions") \
        || die "curl /access/permissions failed (network / PVE down?)"
    status="${body##*__STATUS:}"
    body="${body%__STATUS:*}"
    [[ "$status" == "200" ]] \
        || die "/access/permissions returned HTTP $status; token auth broken"

    # Confirm the token actually has '/' in its permissions map (not just
    # that the auth header parsed). Empty {} means the token has zero ACL.
    if ! echo "$body" | jq -e '.data | type=="object" and length>0' >/dev/null; then
        die "token authenticated but has NO permissions — did the ACL grant " \
            "land on the user instead of the token? Check 'pveum acl list' " \
            "shows a /token/${PVE_USER}@${REALM}!${TOKEN_NAME} entry."
    fi
    log "        ok — token has permissions: $(echo "$body" | jq -c '.data | keys')"

    log "[validation 2/2] GET /cluster/nextid"
    status=$(curl --silent --insecure --max-time 10 \
        --write-out '%{http_code}' --output /dev/null \
        -H "$auth" "${base}/cluster/nextid") \
        || die "curl /cluster/nextid failed"
    [[ "$status" == "200" ]] \
        || die "/cluster/nextid returned HTTP $status; token lacks Datastore.Audit or similar"
    log "        ok"
}

# ─── 5. Output ────────────────────────────────────────────────────────────
print_summary() {
    if [[ -z "$TOKEN_SECRET" ]]; then
        cat <<EOF

============================================================
Token ${FULL_TOKEN_ID} already exists; secret not re-printed.
Re-run with --rotate to mint a fresh secret.
============================================================
EOF
        return 0
    fi

    local credential="PVEAPIToken=${FULL_TOKEN_ID}=${TOKEN_SECRET}"
    cat <<EOF

============================================================
Proxmox API token ready.

Token ID:       ${FULL_TOKEN_ID}
Role:           ${ROLE} at /
Privilege sep:  1 (token has its own ACL — what we want)

Paste this into secrets/secrets.yaml on your workstation:
------------------------------------------------------------
"proxmox/api-token": "${credential}"
------------------------------------------------------------

OR — if you keep a per-host overlay:
------------------------------------------------------------
<your-vm-hostname>:
  "proxmox/api-token": "${credential}"
------------------------------------------------------------

The credential string IS the secret. It cannot be recovered
from Proxmox later. Save it now.
============================================================
EOF

    if [[ -n "$WRITE_SECRET_TO" ]]; then
        umask 077
        printf '"proxmox/api-token": "%s"\n' "$credential" > "$WRITE_SECRET_TO"
        log "wrote credential to $WRITE_SECRET_TO (mode 0600)"
    fi
}

# ─── main ─────────────────────────────────────────────────────────────────
sanity_checks
ensure_user
ensure_token
ensure_acl
validate
print_summary
