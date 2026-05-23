export image_name := env("IMAGE_NAME", "server4home")
export default_tag := env("DEFAULT_TAG", "stable")
export bib_image := env("BIB_IMAGE", "quay.io/centos-bootc/bootc-image-builder:latest@sha256:903c01d110b8533f8891f07c69c0ba2377f8d4bc7e963311082b7028c04d529d")

alias build-vm := build-qcow2
alias rebuild-vm := rebuild-qcow2
alias run-vm := run-vm-qcow2

[private]
default:
    @just --list

# Check Just Syntax
[group('Just')]
check:
    #!/usr/bin/bash
    find . -type f -name "*.just" | while read -r file; do
    	echo "Checking syntax: $file"
    	just --unstable --fmt --check -f $file
    done
    echo "Checking syntax: Justfile"
    just --unstable --fmt --check -f Justfile

# Fix Just Syntax
[group('Just')]
fix:
    #!/usr/bin/bash
    find . -type f -name "*.just" | while read -r file; do
    	echo "Checking syntax: $file"
    	just --unstable --fmt -f $file
    done
    echo "Checking syntax: Justfile"
    just --unstable --fmt -f Justfile || { exit 1; }

# Clean Repo
[group('Utility')]
clean:
    #!/usr/bin/bash
    set -eoux pipefail
    touch _build
    find *_build* -exec rm -rf {} \;
    rm -f previous.manifest.json
    rm -f changelog.md
    rm -f output.env
    rm -rf output/

# Sudo Clean Repo
[group('Utility')]
[private]
sudo-clean:
    just sudoif just clean

# sudoif bash function
[group('Utility')]
[private]
sudoif command *args:
    #!/usr/bin/bash
    function sudoif(){
        if [[ "${UID}" -eq 0 ]]; then
            "$@"
        elif [[ "$(command -v sudo)" && -n "${SSH_ASKPASS:-}" ]] && [[ -n "${DISPLAY:-}" || -n "${WAYLAND_DISPLAY:-}" ]]; then
            /usr/bin/sudo --askpass "$@" || exit 1
        elif [[ "$(command -v sudo)" ]]; then
            /usr/bin/sudo "$@" || exit 1
        else
            exit 1
        fi
    }
    sudoif {{ command }} {{ args }}

# This Justfile recipe builds a container image using Podman.
#
# Arguments:
#   $target_image - The tag you want to apply to the image (default: $image_name).
#   $tag - The tag for the image (default: $default_tag).
#
# The script constructs the version string using the tag and the current date.
# If the git working directory is clean, it also includes the short SHA of the current HEAD.
#
# just build $target_image $tag
#
# Example usage:
#   just build aurora lts
#
# This will build an image 'aurora:lts' with DX and GDX enabled.
#

# Build the image using the specified parameters
build $target_image=image_name $tag=default_tag:
    #!/usr/bin/env bash

    BUILD_ARGS=()
    if [[ -z "$(git status -s)" ]]; then
        BUILD_ARGS+=("--build-arg" "SHA_HEAD_SHORT=$(git rev-parse --short HEAD)")
    fi

    podman build \
        "${BUILD_ARGS[@]}" \
        --pull=newer \
        --tag "${target_image}:${tag}" \
        .

# Build the K3s flavor (server4home-k3s) layered on top of the base image.
# Mode (server/agent) is decided at runtime via /etc/server4home/k3s.conf.
[group('Build K3s Flavor')]
build-k3s $tag=default_tag $k3s_version="v1.35.4+k3s1": (build image_name tag)
    #!/usr/bin/env bash
    set -euo pipefail
    podman build \
        --build-arg "BASE_IMAGE=localhost/${image_name}:${tag}" \
        --build-arg "K3S_VERSION=${k3s_version}" \
        --pull=newer \
        --file Containerfile.k3s \
        --tag "${image_name}-k3s:${tag}" \
        .

# Build a QCOW2 disk image of the K3s flavor (assumes the container image exists)
[group('Build K3s Flavor')]
build-vm-k3s $tag=default_tag: && (_build-bib ("localhost/" + image_name + "-k3s") tag "qcow2" "iso/disk.toml")

# Force-rebuild the container image AND the K3s QCOW2 disk image
[group('Build K3s Flavor')]
rebuild-vm-k3s $tag=default_tag: (build-k3s tag) && (_build-bib ("localhost/" + image_name + "-k3s") tag "qcow2" "iso/disk.toml")

# Ensure a project virtualenv exists at ./.venv with the deploy runner AND
# the helm/kubectl binaries the runner shells out to. All three live under
# .venv/bin/ and recipes prepend that to PATH, so atomic-OS workstations
# don't need rpm-ostree installs or reboots.
# Idempotent: a stamp file tracks the last pip install; the binary downloads
# self-check via -x test.
[private]
_python-env:
    #!/usr/bin/env bash
    set -euo pipefail
    HELM_VERSION="${HELM_VERSION:-v3.21.0}"
    KUBECTL_VERSION="${KUBECTL_VERSION:-v1.35.4}"

    stamp=".venv/.installed"
    if [[ ! -d .venv ]]; then
        echo ">>> Creating ./.venv (Python virtualenv)"
        python3 -m venv .venv
    fi
    if [[ ! -f "$stamp" ]] || [[ "tools/pyproject.toml" -nt "$stamp" ]]; then
        echo ">>> Installing server4home (editable) into .venv"
        .venv/bin/pip install --quiet --upgrade pip
        .venv/bin/pip install --quiet -e tools
        touch "$stamp"
    fi
    if [[ ! -x .venv/bin/helm ]]; then
        echo ">>> Bootstrapping helm ${HELM_VERSION} into .venv/bin/"
        tmp="$(mktemp -d)"
        trap "rm -rf '$tmp'" EXIT
        curl -fsSL "https://get.helm.sh/helm-${HELM_VERSION}-linux-amd64.tar.gz" \
            | tar -xz -C "$tmp"
        install -m 0755 "$tmp/linux-amd64/helm" .venv/bin/helm
    fi
    if [[ ! -x .venv/bin/kubectl ]]; then
        echo ">>> Bootstrapping kubectl ${KUBECTL_VERSION} into .venv/bin/"
        curl -fsSL -o .venv/bin/kubectl \
            "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl"
        chmod 0755 .venv/bin/kubectl
    fi

# Examples:
#     just deploy instances/k3s-on-virt-manager.yaml
#     just deploy instances/foo.yaml

# Deploy a VM from an instance manifest (tools/server4home Python runner)
[group('Deploy')]
deploy manifest: _python-env
    PATH="$PWD/.venv/bin:$PATH" .venv/bin/server4home deploy {{ manifest }}

# Use when the cluster identity has changed (different hostname): the
# preserved per-VM data disk + identity meta are dropped before deploying.

# Deploy + drop preserved per-VM data disk (use when identity changed)
[group('Deploy')]
deploy-fresh manifest: _python-env
    PATH="$PWD/.venv/bin:$PATH" .venv/bin/server4home deploy --wipe-data {{ manifest }}

# Tear down a VM by manifest. Prompts for confirmation.
[group('Deploy')]
destroy manifest: _python-env
    PATH="$PWD/.venv/bin:$PATH" .venv/bin/server4home destroy {{ manifest }}

# Validate a manifest without doing anything.
[group('Deploy')]
validate manifest: _python-env
    PATH="$PWD/.venv/bin:$PATH" .venv/bin/server4home validate {{ manifest }}

# Show every registered plugin (targets, provisioners, installers).
[group('Deploy')]
list-plugins: _python-env
    PATH="$PWD/.venv/bin:$PATH" .venv/bin/server4home list-plugins

# Re-render docs/deployment-history.md from deployments/*.json (the JSON
# files are the source of truth; the markdown is a generated, committed
# artifact so PR diffs and offline grep work).
[group('Deploy')]
history: _python-env
    PATH="$PWD/.venv/bin:$PATH" .venv/bin/server4home history render

# CI-style check: fails if docs/deployment-history.md is stale.
[group('Deploy')]
history-check: _python-env
    PATH="$PWD/.venv/bin:$PATH" .venv/bin/server4home history check

# Command: _rootful_load_image
# Description: This script checks if the current user is root or running under sudo. If not, it attempts to resolve the image tag using podman inspect.
#              If the image is found, it loads it into rootful podman. If the image is not found, it pulls it from the repository.
#
# Parameters:
#   $target_image - The name of the target image to be loaded or pulled.
#   $tag - The tag of the target image to be loaded or pulled. Default is 'default_tag'.
#
# Example usage:
#   _rootful_load_image my_image latest
#
# Steps:
# 1. Check if the script is already running as root or under sudo.
# 2. Check if target image is in the non-root podman container storage)
# 3. If the image is found, load it into rootful podman using podman scp.
# 4. If the image is not found, pull it from the remote repository into reootful podman.

_rootful_load_image $target_image=image_name $tag=default_tag:
    #!/usr/bin/bash
    set -eoux pipefail

    # Check if already running as root or under sudo
    if [[ -n "${SUDO_USER:-}" || "${UID}" -eq "0" ]]; then
        echo "Already root or running under sudo, no need to load image from user podman."
        exit 0
    fi

    # Try to resolve the image tag using podman inspect
    set +e
    resolved_tag=$(podman inspect -t image "${target_image}:${tag}" | jq -r '.[].RepoTags.[0]')
    return_code=$?
    set -e

    USER_IMG_ID=$(podman images --filter reference="${target_image}:${tag}" --format "'{{ '{{.ID}}' }}'")

    if [[ $return_code -eq 0 ]]; then
        # If the image is found, load it into rootful podman
        ID=$(just sudoif podman images --filter reference="${target_image}:${tag}" --format "'{{ '{{.ID}}' }}'")
        if [[ "$ID" != "$USER_IMG_ID" ]]; then
            # If the image ID is not found or different from user, copy the image from user podman to root podman
            COPYTMP=$(mktemp -p "${PWD}" -d -t _build_podman_scp.XXXXXXXXXX)
            just sudoif TMPDIR=${COPYTMP} podman image scp ${UID}@localhost::"${target_image}:${tag}" root@localhost::"${target_image}:${tag}"
            rm -rf "${COPYTMP}"
        fi
    else
        # If the image is not found, pull it from the repository
        just sudoif podman pull "${target_image}:${tag}"
    fi

# Build a bootc bootable image using Bootc Image Builder (BIB)
# Converts a container image to a bootable image
# Parameters:
#   target_image: The name of the image to build (ex. localhost/fedora)
#   tag: The tag of the image to build (ex. latest)
#   type: The type of image to build (ex. qcow2, raw, iso)
#   config: The configuration file to use for the build (default: iso/disk.toml)

# Example: just _rebuild-bib localhost/fedora latest qcow2 iso/disk.toml
_build-bib $target_image $tag $type $config: (_rootful_load_image target_image tag)
    #!/usr/bin/env bash
    set -euo pipefail

    args="--type ${type} "
    args+="--use-librepo=True "
    args+="--rootfs=xfs"

    BUILDTMP=$(mktemp -p "${PWD}" -d -t _build-bib.XXXXXXXXXX)

    sudo podman run \
      --rm \
      -it \
      --privileged \
      --pull=newer \
      --net=host \
      --security-opt label=type:unconfined_t \
      -v $(pwd)/${config}:/config.toml:ro \
      -v $BUILDTMP:/output \
      -v /var/lib/containers/storage:/var/lib/containers/storage \
      "${bib_image}" \
      ${args} \
      "${target_image}:${tag}"

    mkdir -p output
    # BIB writes its output into per-type subdirs (e.g. output/qcow2/disk.qcow2).
    # `mv -f` does not replace non-empty directories, so clear the type-specific
    # output dir first if a prior build of the same type left one behind.
    if [[ "${type}" == "iso" ]]; then
        sudo rm -rf output/bootiso
    else
        sudo rm -rf "output/${type}"
    fi
    sudo mv -f $BUILDTMP/* output/
    sudo rmdir $BUILDTMP
    sudo chown -R $USER:$USER output/

# Podman builds the image from the Containerfile and creates a bootable image
# Parameters:
#   target_image: The name of the image to build (ex. localhost/fedora)
#   tag: The tag of the image to build (ex. latest)
#   type: The type of image to build (ex. qcow2, raw, iso)
#   config: The configuration file to use for the build (deafult: iso/disk.toml)

# Example: just _rebuild-bib localhost/fedora latest qcow2 iso/disk.toml
_rebuild-bib $target_image $tag $type $config: (build target_image tag) && (_build-bib target_image tag type config)

# Build a QCOW2 virtual machine image
[group('Build Virtal Machine Image')]
build-qcow2 $target_image=("localhost/" + image_name) $tag=default_tag: && (_build-bib target_image tag "qcow2" "iso/disk.toml")

# Build a RAW virtual machine image
[group('Build Virtal Machine Image')]
build-raw $target_image=("localhost/" + image_name) $tag=default_tag: && (_build-bib target_image tag "raw" "iso/disk.toml")

# Build an ISO virtual machine image
[group('Build Virtal Machine Image')]
build-iso $target_image=("localhost/" + image_name) $tag=default_tag: && (_build-bib target_image tag "iso" "iso/iso.toml")

# Rebuild a QCOW2 virtual machine image
[group('Build Virtal Machine Image')]
rebuild-qcow2 $target_image=("localhost/" + image_name) $tag=default_tag: && (_rebuild-bib target_image tag "qcow2" "iso/disk.toml")

# Rebuild a RAW virtual machine image
[group('Build Virtal Machine Image')]
rebuild-raw $target_image=("localhost/" + image_name) $tag=default_tag: && (_rebuild-bib target_image tag "raw" "iso/disk.toml")

# Rebuild an ISO virtual machine image
[group('Build Virtal Machine Image')]
rebuild-iso $target_image=("localhost/" + image_name) $tag=default_tag: && (_rebuild-bib target_image tag "iso" "iso/iso.toml")

# Run a virtual machine with the specified image type and configuration
# The VM joins your LAN via DHCP through a macvlan network (the VM gets its
# own IP from your router). Override the LAN_* vars below if your network
# differs. Note: due to macvlan design, the host running this command cannot
# reach the VM directly — SSH from another machine on the LAN.
_run-vm $target_image $tag $type $config:
    #!/usr/bin/bash
    set -eoux pipefail

    # LAN config for macvlan networking. Override via environment if needed.
    LAN_SUBNET="${LAN_SUBNET:-192.168.0.0/16}"
    LAN_GATEWAY="${LAN_GATEWAY:-192.168.1.1}"
    LAN_PARENT_IFACE="${LAN_PARENT_IFACE:-br0}"
    NETWORK_NAME="${NETWORK_NAME:-qemu-lan}"

    # Determine the image file based on the type
    image_file="output/${type}/disk.${type}"
    if [[ $type == iso ]]; then
        image_file="output/bootiso/install.iso"
    fi

    # Build the image if it does not exist
    if [[ ! -f "${image_file}" ]]; then
        just "build-${type}" "$target_image" "$tag"
    fi

    # Ensure the macvlan network exists (idempotent, rootful)
    if ! sudo podman network exists "$NETWORK_NAME"; then
        sudo podman network create -d macvlan \
            --subnet="$LAN_SUBNET" \
            --gateway="$LAN_GATEWAY" \
            -o parent="$LAN_PARENT_IFACE" \
            "$NETWORK_NAME"
    fi

    echo "VM will join LAN '$LAN_SUBNET' via DHCP (parent: $LAN_PARENT_IFACE)"
    echo "After boot, find its IP from your router's DHCP leases, then:"
    echo "  ssh developer@<vm-ip>   (from another machine on the LAN)"

    # Set up the arguments for running the VM
    run_args=()
    run_args+=(--rm --privileged)
    run_args+=(--pull=newer)
    run_args+=(--network "$NETWORK_NAME")
    run_args+=(--env "DHCP=Y")
    run_args+=(--env "CPU_CORES=4")
    run_args+=(--env "RAM_SIZE=8G")
    run_args+=(--env "DISK_SIZE=64G")
    run_args+=(--env "TPM=Y")
    run_args+=(--env "GPU=Y")
    run_args+=(--cap-add=NET_ADMIN)
    run_args+=(--device=/dev/kvm)
    run_args+=(--device=/dev/net/tun)
    run_args+=(--device=/dev/vhost-net)
    run_args+=(--device-cgroup-rule="c *:* rwm")
    run_args+=(--volume "${PWD}/${image_file}":"/boot.${type}")
    run_args+=(docker.io/qemux/qemu)

    # Run the VM (rootful: macvlan + /dev/vhost-net require it)
    sudo podman run "${run_args[@]}"

# Run a virtual machine from a QCOW2 image
[group('Run Virtal Machine')]
run-vm-qcow2 $target_image=("localhost/" + image_name) $tag=default_tag: && (_run-vm target_image tag "qcow2" "iso/disk.toml")

# Run a virtual machine from a RAW image
[group('Run Virtal Machine')]
run-vm-raw $target_image=("localhost/" + image_name) $tag=default_tag: && (_run-vm target_image tag "raw" "iso/disk.toml")

# Run a virtual machine from an ISO
[group('Run Virtal Machine')]
run-vm-iso $target_image=("localhost/" + image_name) $tag=default_tag: && (_run-vm target_image tag "iso" "iso/iso.toml")

# Attaches the VM to the host bridge (default: br0), so the VM is a peer on
# your LAN — reachable from every host on the network, including this one.
# Re-running tears down any previous domain with the same name and re-imports.
#
# If data_disk_size is non-empty, a second blank qcow2 is attached as vdb and
# the K3s first-boot service will claim it for /var/lib/rancher. An existing
# data disk at the expected path is preserved on re-imports (delete it
# manually with `sudo rm` if you want a clean slate).
#
# Low-level libvirt import. Most workflows should use `just deploy <manifest>`
# instead — this recipe is the manifest-less escape hatch for debugging.
#
# Parameters:
#   vm_name:        libvirt domain name (default: $image_name)
#   memory:         RAM in MB (default: 8192)
#   vcpus:          number of vCPUs (default: 4)
#   bridge:         host bridge interface (default: br0)
#   data_disk_size: e.g. "100G" to attach a data disk; empty for none (default: "")

# Import the built qcow2 into libvirt as a managed domain
[group('Libvirt')]
import-libvirt $vm_name=image_name $memory="8192" $vcpus="4" $bridge="br0" $data_disk_size="":
    #!/usr/bin/env bash
    set -euo pipefail

    src="output/qcow2/disk.qcow2"
    dst="/var/lib/libvirt/images/${vm_name}.qcow2"
    data_dst="/var/lib/libvirt/images/${vm_name}-data.qcow2"

    if [[ ! -f "$src" ]]; then
        echo "Error: $src not found. Run 'just build-vm' first." >&2
        exit 1
    fi

    sudo systemctl enable --now libvirtd.socket

    if sudo virsh dominfo "$vm_name" >/dev/null 2>&1; then
        echo "Domain '$vm_name' exists; destroying and undefining (keeping NVRAM clean)."
        sudo virsh destroy "$vm_name" 2>/dev/null || true
        sudo virsh undefine "$vm_name" --nvram 2>/dev/null \
          || sudo virsh undefine "$vm_name"
    fi

    sudo cp -f "$src" "$dst"
    sudo chown qemu:qemu "$dst"

    # Build the disk arg list. Boot disk first, then optional data disk.
    disk_args=("--disk" "path=$dst,format=qcow2,bus=virtio")

    if [[ -n "$data_disk_size" ]]; then
        if [[ -f "$data_dst" ]]; then
            echo "Reusing existing data disk: $data_dst (delete it manually for a clean slate)."
        else
            echo "Creating data disk: $data_dst ($data_disk_size)"
            sudo qemu-img create -f qcow2 "$data_dst" "$data_disk_size"
            sudo chown qemu:qemu "$data_dst"
        fi
        disk_args+=("--disk" "path=$data_dst,format=qcow2,bus=virtio")
    fi

    # SMBIOS carries the vm_name as system.product so the image's
    # set-hostname.sh produces "<vm_name>-<8hex of machine-id>" (prefix mode).
    sysinfo="smbios,system.manufacturer=server4home,system.product=$vm_name"

    sudo virt-install \
      --name "$vm_name" \
      --memory "$memory" \
      --vcpus "$vcpus" \
      "${disk_args[@]}" \
      --import \
      --os-variant fedora-unknown \
      --network "bridge=$bridge,model=virtio" \
      --sysinfo "$sysinfo" \
      --boot uefi \
      --tpm model=tpm-crb,backend.type=emulator,backend.version=2.0 \
      --graphics spice \
      --noautoconsole

    echo ""
    echo "Domain '$vm_name' imported and starting."
    echo "Find its DHCP lease on your router, then: ssh developer@<vm-ip>"
    echo "Or open Cockpit Client -> localhost -> Virtual Machines."

# Run a virtual machine using systemd-vmspawn
[group('Run Virtal Machine')]
spawn-vm rebuild="0" type="qcow2" ram="6G":
    #!/usr/bin/env bash

    set -euo pipefail

    [ "{{ rebuild }}" -eq 1 ] && echo "Rebuilding the ISO" && just build-vm {{ rebuild }} {{ type }}

    systemd-vmspawn \
      -M "bootc-image" \
      --console=gui \
      --cpus=2 \
      --ram=$(echo {{ ram }}| /usr/bin/numfmt --from=iec) \
      --network-user-mode \
      --vsock=false --pass-ssh-key=false \
      -i ./output/**/*.{{ type }}

# Runs shell check on all Bash scripts
lint:
    #!/usr/bin/env bash
    set -eoux pipefail
    # Check if shellcheck is installed
    if ! command -v shellcheck &> /dev/null; then
        echo "shellcheck could not be found. Please install it."
        exit 1
    fi
    # Run shellcheck on all Bash scripts
    /usr/bin/find . -iname "*.sh" -type f -exec shellcheck "{}" ';'

# Runs shfmt on all Bash scripts
format:
    #!/usr/bin/env bash
    set -eoux pipefail
    # Check if shfmt is installed
    if ! command -v shfmt &> /dev/null; then
        echo "shellcheck could not be found. Please install it."
        exit 1
    fi
    # Run shfmt on all Bash scripts
    /usr/bin/find . -iname "*.sh" -type f -exec shfmt --write "{}" ';'
