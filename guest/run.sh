#!/usr/bin/env bash
set -euo pipefail

SHARE=/mnt/host
RUN_ID=unknown
SCENARIO=unknown
STATUS=FAIL
REASON=initialization_failed
TOOLS='{}'
KERNEL_RELEASE=$(uname -r)
BOOT_ID=$(cat /proc/sys/kernel/random/boot_id)
SHARE_READY=0

finish() {
    local rc=$?
    trap - EXIT ERR
    set +e
    if [[ $rc -eq 78 ]]; then STATUS=BLOCKED; elif [[ $rc -ne 0 ]]; then STATUS=FAIL; fi
    if [[ $SHARE_READY -eq 1 ]]; then
        if ! dmesg > "$SHARE/dmesg.log" 2>&1; then
            STATUS=FAIL
            REASON=dmesg_capture_failed
        fi
        if ! jq -n --arg run_id "$RUN_ID" --arg scenario "$SCENARIO" --arg status "$STATUS" --arg reason "$REASON" \
            --arg kernel_release "$KERNEL_RELEASE" --arg boot_id "$BOOT_ID" \
            --arg cmdline "$(cat /proc/cmdline)" --argjson tools "$TOOLS" \
            '{schema_version: 1, run_id: $run_id, scenario: $scenario, status: $status,
              reason: $reason, kernel_release: $kernel_release, boot_id: $boot_id,
              cmdline: $cmdline, tools: $tools,
              evidence_basis: "guest_kernel_measured"}' > "$SHARE/guest.json.tmp" ||
           ! sync "$SHARE/guest.json.tmp" "$SHARE/dmesg.log" ||
           ! mv -f "$SHARE/guest.json.tmp" "$SHARE/guest.json"; then
            STATUS=FAIL
        fi
    fi
    printf 'TIERLAB_DONE %s %s\n' "$RUN_ID" "$STATUS"
    sync
    systemctl --no-block poweroff
    if [[ $STATUS == PASS && $rc -eq 0 ]]; then exit 0; fi
    if [[ $STATUS == BLOCKED ]]; then exit 78; fi
    exit 1
}
fail() { REASON=$1; printf 'ERROR: %s\n' "$REASON" >&2; exit 1; }

# 在任何設定以前先辨識 Guest；migrate／policy／offline scenario 才可配置 CXL。
[[ $KERNEL_RELEASE != *microsoft* && $KERNEL_RELEASE != *Microsoft* ]] \
    || fail wrong_kernel_domain
[[ $KERNEL_RELEASE == 6.18.56 && $EUID -eq 0 ]] || fail wrong_guest_version_or_user
VENDOR=$(cat /sys/class/dmi/id/sys_vendor)
PRODUCT=$(cat /sys/class/dmi/id/product_name)
[[ $VENDOR == *QEMU* || $PRODUCT == *QEMU* ]] || fail not_qemu_guest
# 身分驗證失敗時不能進入含 poweroff 的 Guest cleanup。
trap finish EXIT
trap 'REASON=command_failed' ERR
mkdir -p "$SHARE"
mount -t 9p -o trans=virtio,version=9p2000.L host0 "$SHARE"
[[ $(findmnt -n -o FSTYPE "$SHARE") == 9p ]] || fail missing_run_share
SHARE_READY=1
[[ -f $SHARE/config.json && $(stat -c%s "$SHARE/config.json") -le 65536 ]] \
    || fail invalid_config_file
jq -e '.schema_version == 1 and (.scenario == "boot" or .scenario == "migrate" or .scenario == "policy" or .scenario == "offline" or .scenario == "trace") and
       (.run_id | type == "string" and test("^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$"))' \
    "$SHARE/config.json" >/dev/null || fail invalid_boot_config
RUN_ID=$(jq -r .run_id "$SHARE/config.json")
SCENARIO=$(jq -r .scenario "$SHARE/config.json")
for tool in cxl daxctl ndctl; do
    path=$(command -v "$tool") || fail missing_guest_tool
    version=$("$tool" --version) || fail guest_tool_probe_failed
    [[ -n $version ]] || fail empty_guest_tool_version
    TOOLS=$(jq --arg name "$tool" --arg path "$path" --arg version "$version" \
        '.[$name] = {path: $path, version: $version}' <<< "$TOOLS")
done
[[ -n $BOOT_ID ]] || fail missing_boot_id
if [[ $SCENARIO == migrate || $SCENARIO == policy || $SCENARIO == offline || $SCENARIO == trace ]]; then
    [[ -x $SHARE/tierlab ]] || fail missing_guest_binary
    "$SHARE/tierlab" --help > "$SHARE/tierlab-help.log" 2>&1 || fail guest_runtime_unavailable
    # 觀測模式停用額外自動搬移；此處已通過 Guest 身分驗證。
    if [[ -e /sys/kernel/mm/ksm/run ]]; then echo 0 > /sys/kernel/mm/ksm/run; fi
    if [[ -e /proc/sys/kernel/numa_balancing ]]; then echo 0 > /proc/sys/kernel/numa_balancing; fi
    if [[ -e /sys/kernel/mm/numa/demotion_enabled ]]; then echo false > /sys/kernel/mm/numa/demotion_enabled; fi
    if python3 /usr/local/libexec/tierlab/cxl_setup.py --output "$SHARE" --run-id "$RUN_ID"; then
        FAST=$(jq -er '.fast_node' "$SHARE/topology.json")
        SLOW=$(jq -er '.slow_node' "$SHARE/topology.json")
    else
        rc=$?
        REASON=cxl_setup_failed
        exit "$rc"
    fi
    if [[ $SCENARIO == trace ]]; then
        command=(python3 /usr/local/libexec/tierlab/trace.py --output "$SHARE" --run-id "$RUN_ID"
                 --fast-node "$FAST" --slow-node "$SLOW" --binary "$SHARE/tierlab" --baseline "$SHARE/upstream-migration")
    else
        command=("$SHARE/tierlab" "$SCENARIO" --fast-node "$FAST" --slow-node "$SLOW" --output "$SHARE" --run-id "$RUN_ID")
    fi
    if "${command[@]}"; then
        REASON=${SCENARIO}_complete
    else
        rc=$?
        REASON=${SCENARIO}_failed
        exit "$rc"
    fi
    if [[ $SCENARIO == offline ]]; then
        if python3 /usr/local/libexec/tierlab/cxl_offline.py --output "$SHARE" --run-id "$RUN_ID"; then
            REASON=offline_complete
        else
            rc=$?
            REASON=offline_failed
            exit "$rc"
        fi
    fi
else
    REASON=boot_complete
fi
STATUS=PASS
printf 'Guest %s scenario completed: %s\n' "$SCENARIO" "$RUN_ID"
