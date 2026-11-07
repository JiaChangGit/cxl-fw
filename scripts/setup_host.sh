#!/usr/bin/env bash
set -euo pipefail

fail() { printf 'ERROR: %s\n' "$1" >&2; exit 78; }
[[ $# -eq 1 ]] || fail 'Usage: setup_host.sh TARGET_USER (run as root)'
[[ $(uname -s) == Linux ]] || fail 'Linux is required'
[[ $EUID -eq 0 ]] || fail 'Run as root; do not put passwords in commands'
target_user=$1
[[ $target_user =~ ^[a-z_][a-z0-9_-]{0,31}$ ]] || fail 'Invalid target user'
getent passwd "$target_user" >/dev/null || fail 'Target user does not exist'
[[ $(id -u "$target_user") -ne 0 ]] || fail 'Target must be a normal user'

# 僅接受專案使用的發行版，避免套件與指令語意漂移。
# shellcheck source=/dev/null
source /etc/os-release
[[ $ID == ubuntu && $VERSION_ID == 26.04 ]] || fail 'Ubuntu 26.04 is required'
getent group kvm >/dev/null || fail 'KVM group is missing; check host virtualization'
script_dir=$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
package_file=$script_dir/../configs/host-packages.txt
[[ -f $package_file ]] || fail 'Missing configs/host-packages.txt'
missing=()
while IFS= read -r package || [[ -n $package ]]; do
    [[ -n $package && $package != \#* ]] || continue
    [[ $package =~ ^[a-z0-9][a-z0-9+.-]*$ ]] || fail 'Invalid package name'
    status=$(dpkg-query -W -f='${db:Status-Abbrev}' -- "$package" 2>/dev/null || true)
    [[ $status == ii* ]] || missing+=("$package")
done < "$package_file"

if ((${#missing[@]})); then
    printf 'Installing %s missing packages\n' "${#missing[@]}"
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${missing[@]}"
else
    printf 'All Host packages are already installed\n'
fi
usermod -aG kvm "$target_user"
printf 'Host setup complete for %s; use a fresh user process, then run make doctor\n' "$target_user"
