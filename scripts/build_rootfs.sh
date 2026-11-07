#!/usr/bin/env bash
set -euo pipefail

fail() { printf 'ERROR: %s\n' "$1" >&2; exit 78; }
[[ $EUID -eq 0 ]] || fail 'Run this rootfs builder as root'
[[ $# -eq 0 ]] || fail 'Usage: build_rootfs.sh'

ROOT=$(realpath -- "$(dirname -- "$0")/..")
TREE=$ROOT/build/rootfs-tree
KOUT=$ROOT/build/kernel
KSRC=$ROOT/build/sources/linux-6.18.56
IMAGE=$ROOT/images/rootfs.ext4
META=$ROOT/images/rootfs.meta.json
PACKAGES=$ROOT/images/rootfs.packages.tsv
LOCK=$ROOT/configs/versions.lock.json
GUEST_PACKAGES=bash,coreutils,findutils,systemd-sysv,udev,cxl,daxctl,ndctl,numactl,jq,kmod,util-linux,procps,python3,libstdc++6
for path in "$TREE" "$KOUT" "$KSRC" "$IMAGE" "$META" "$PACKAGES"; do
    [[ $(realpath -m -- "$path") == "$path" ]] || fail "Symlinked build path: $path"
done

for command in debootstrap mkfs.ext4 depmod chroot flock jq sha256sum; do
    command -v "$command" >/dev/null || fail "Missing executable: $command"
done
for file in "$KOUT/arch/x86/boot/bzImage" "$KOUT/.config" \
            "$KOUT/include/config/kernel.release" "$KOUT/modules.builtin" \
            "$KOUT/modules.builtin.modinfo" "$ROOT/guest/run.sh" \
            "$ROOT/guest/tierlab.service" "$ROOT/guest/cxl_setup.py" "$ROOT/guest/cxl_offline.py" "$ROOT/guest/trace.py" "$LOCK"; do
    [[ -f $file ]] || fail "Missing input: $file"
done
jq -e '.linux.version == "6.18.56" and
       (.linux.source_sha256 | type == "string" and test("^[a-fA-F0-9]{64}$")) and
       .rootfs.suite == "resolute" and .rootfs.arch == "amd64"' "$LOCK" >/dev/null \
    || fail 'Rootfs inputs must use the verified pinned kernel and resolute amd64'
KREL=$(cat "$KOUT/include/config/kernel.release")
[[ $KREL == 6.18.56 ]] || fail "Unexpected kernel release: $KREL"
[[ -f $KSRC/Makefile ]] || fail 'Missing pinned kernel source'

mkdir -p "$ROOT/build/logs" "$ROOT/images"
touch "$ROOT/build/heavy.lock"
chown --reference="$ROOT" "$ROOT/build" "$ROOT/build/logs" \
    "$ROOT/images" "$ROOT/build/heavy.lock"
exec 9<>"$ROOT/build/heavy.lock"
flock -x 9
exec > >(tee -a "$ROOT/build/logs/rootfs.log") 2>&1

# 只將穩定的來源與內容納入 key，不讓階段狀態文字造成無意義重建。
BUILD_KEY=$(
    {
        jq -Sc '{linux: {version: .linux.version, source_sha256: .linux.source_sha256},
                 rootfs: {suite: .rootfs.suite, arch: .rootfs.arch}}' "$LOCK"
        sha256sum "$0" "$ROOT/guest/run.sh" "$ROOT/guest/tierlab.service" "$ROOT/guest/cxl_setup.py" "$ROOT/guest/cxl_offline.py" "$ROOT/guest/trace.py" \
            "$KOUT/arch/x86/boot/bzImage" "$KOUT/.config" \
            "$KOUT/modules.builtin" "$KOUT/modules.builtin.modinfo" | awk '{print $1}'
        printf '%s\n' "$GUEST_PACKAGES"
    } | sha256sum | awk '{print $1}'
)

if [[ -e $IMAGE || -e $META ]]; then
    [[ -f $IMAGE && -f $META && -f $PACKAGES ]] \
        || fail 'Incomplete rootfs artifacts; inspect them before rebuilding'
    [[ $(sha256sum "$IMAGE" | awk '{print $1}') == $(jq -r .image_sha256 "$META") ]] \
        || fail 'Existing base image differs from its manifest'
    [[ $(sha256sum "$PACKAGES" | awk '{print $1}') == $(jq -r .package_manifest_sha256 "$META") ]] \
        || fail 'Existing package manifest differs from its metadata'
    if [[ $(jq -r .build_key "$META") == "$BUILD_KEY" ]]; then
        printf 'Reusing verified rootfs: %s\n' "$IMAGE"
        exit 0
    fi
    # Retain the prior immutable image when Guest inputs require a new base.
    prior_sha=$(jq -r .image_sha256 "$META")
    prior_image=$ROOT/images/rootfs-$prior_sha.ext4
    if [[ ! -e $prior_image ]]; then
        ln -- "$IMAGE" "$prior_image"
        cp -- "$META" "$ROOT/images/rootfs-$prior_sha.meta.json"
        cp -- "$PACKAGES" "$ROOT/images/rootfs-$prior_sha.packages.tsv"
    fi
fi
[[ ! -e $IMAGE.tmp && ! -e $META.tmp ]] \
    || fail 'A previous temporary image/manifest remains; inspect the failed build'

if [[ -e $TREE ]]; then
    [[ -f $TREE/.tierlab-bootstrap-complete ]] \
        || fail 'Incomplete rootfs tree preserved; inspect it before retrying'
    [[ $(cat "$TREE/.tierlab-bootstrap-complete") == resolute-amd64:* ]] \
        || fail 'The existing rootfs tree has a different bootstrap identity'
else
    debootstrap --variant=minbase --arch=amd64 --components=main,universe \
        --include="$GUEST_PACKAGES" resolute "$TREE" https://archive.ubuntu.com/ubuntu
    printf 'resolute-amd64:%s\n' "$GUEST_PACKAGES" > "$TREE/.tierlab-bootstrap-complete"
fi

# 擴充已驗證的同一套件樹，僅補缺少項目，不重新刪除或 bootstrap。
IFS=, read -r -a package_names <<< "$GUEST_PACKAGES"
missing=()
for package in "${package_names[@]}"; do
    # dpkg-query expands this field expression; the shell must preserve it.
    # shellcheck disable=SC2016
    state=$(chroot "$TREE" dpkg-query -W -f='${db:Status-Abbrev}' -- "$package" 2>/dev/null || true)
    [[ $state == ii* ]] || missing+=("$package")
done
if ((${#missing[@]})); then
    chroot "$TREE" apt-get update
    chroot "$TREE" env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${missing[@]}"
fi
printf 'resolute-amd64:%s\n' "$GUEST_PACKAGES" > "$TREE/.tierlab-bootstrap-complete"

install -m0755 "$ROOT/guest/run.sh" "$TREE/usr/local/sbin/tierlab-run"
install -Dm0755 "$ROOT/guest/cxl_setup.py" "$TREE/usr/local/libexec/tierlab/cxl_setup.py"
install -Dm0755 "$ROOT/guest/cxl_offline.py" "$TREE/usr/local/libexec/tierlab/cxl_offline.py"
install -Dm0755 "$ROOT/guest/trace.py" "$TREE/usr/local/libexec/tierlab/trace.py"
install -Dm0644 "$ROOT/guest/tierlab.service" "$TREE/etc/systemd/system/tierlab.service"
mkdir -p "$TREE/etc/systemd/system/multi-user.target.wants" "$TREE/mnt/host"
ln -sfn ../tierlab.service "$TREE/etc/systemd/system/multi-user.target.wants/tierlab.service"
printf 'cxl-tierlab\n' > "$TREE/etc/hostname"
printf '/dev/vda / ext4 defaults 0 1\n' > "$TREE/etc/fstab"
truncate -s 0 "$TREE/etc/machine-id"

make -C "$KSRC" O="$KOUT" INSTALL_MOD_PATH="$TREE" INSTALL_MOD_STRIP=1 modules_install
install -m0644 "$KOUT/modules.builtin" "$TREE/lib/modules/$KREL/modules.builtin"
install -m0644 "$KOUT/modules.builtin.modinfo" "$TREE/lib/modules/$KREL/modules.builtin.modinfo"
depmod -b "$TREE" "$KREL"
chroot "$TREE" /bin/sh -ec 'test -x /sbin/init; cxl --version; daxctl --version; ndctl --version'
chroot "$TREE" dpkg-query -W > "$PACKAGES"

# 不掛 loop 裝置；mkfs 直接將 rootfs tree 封裝到新的暫存映像。
truncate -s 3G "$IMAGE.tmp"
mkfs.ext4 -q -F -d "$TREE" "$IMAGE.tmp"
IMAGE_SHA=$(sha256sum "$IMAGE.tmp" | awk '{print $1}')
PACKAGES_SHA=$(sha256sum "$PACKAGES" | awk '{print $1}')
CONFIG_SHA=$(sha256sum "$KOUT/.config" | awk '{print $1}')
RUN_SHA=$(sha256sum "$ROOT/guest/run.sh" | awk '{print $1}')
SETUP_SHA=$(sha256sum "$ROOT/guest/cxl_setup.py" | awk '{print $1}')
OFFLINE_SHA=$(sha256sum "$ROOT/guest/cxl_offline.py" | awk '{print $1}')
TRACE_SHA=$(sha256sum "$ROOT/guest/trace.py" | awk '{print $1}')
UNIT_SHA=$(sha256sum "$ROOT/guest/tierlab.service" | awk '{print $1}')
jq -n --arg build_key "$BUILD_KEY" --arg image_sha256 "$IMAGE_SHA" \
    --arg kernel_release "$KREL" --arg package_manifest_sha256 "$PACKAGES_SHA" \
    --arg kernel_config_sha256 "$CONFIG_SHA" \
    --arg trace_sha "$TRACE_SHA" --arg offline_sha "$OFFLINE_SHA" --arg run_sha "$RUN_SHA" --arg setup_sha "$SETUP_SHA" --arg unit_sha "$UNIT_SHA" \
    '{schema_version: 1, image: "images/rootfs.ext4", build_key: $build_key,
      image_sha256: $image_sha256, kernel_release: $kernel_release,
      kernel_config_sha256: $kernel_config_sha256,
      guest_inputs: {"guest/run.sh": $run_sha, "guest/cxl_setup.py": $setup_sha,
                     "guest/cxl_offline.py": $offline_sha, "guest/trace.py": $trace_sha, "guest/tierlab.service": $unit_sha},
      package_manifest: "images/rootfs.packages.tsv",
      package_manifest_sha256: $package_manifest_sha256}' > "$META.tmp"
chmod 0444 "$IMAGE.tmp"
chmod 0644 "$META.tmp" "$PACKAGES"
chown --reference="$ROOT" "$IMAGE.tmp" "$META.tmp" "$PACKAGES"
mv -T -- "$IMAGE.tmp" "$IMAGE"
mv -T -- "$META.tmp" "$META"
printf 'Rootfs ready: %s (sha256=%s)\n' "$IMAGE" "$IMAGE_SHA"
