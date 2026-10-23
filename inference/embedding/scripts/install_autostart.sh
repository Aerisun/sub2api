#!/usr/bin/env bash
set -Eeuo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "${project_dir}/config.env"
if [[ "${EMBED_BACKEND_MODE:-native}" == "pool" ]]; then
    export XDG_RUNTIME_DIR="/run/user/$(id -u)"
    export DBUS_SESSION_BUS_ADDRESS="unix:path=${XDG_RUNTIME_DIR}/bus"
    unit_dir="${HOME}/.config/systemd/user"
    mkdir -p "${unit_dir}"
    for unit in localinference.slice localinference-embedding.slice qwen3-embedding.service qwen3-embedding-worker@.service; do
        install -m 600 "${project_dir}/systemd/${unit}" "${unit_dir}/${unit}"
    done
    python3 - "${project_dir}" <<'PY'
import subprocess, sys
entry = '@reboot ' + sys.argv[1] + '/scripts/supervisor_start.sh'
old = subprocess.run(['crontab', '-l'], capture_output=True, text=True)
if old.returncode == 0:
    lines = old.stdout.splitlines()
    kept = [line for line in lines if line != entry]
    if lines != kept:
        subprocess.run(['crontab', '-'], input='\n'.join(kept) + '\n', text=True, check=True)
PY
    systemctl --user daemon-reload
    systemctl --user enable --now qwen3-embedding.service
    echo "CPU embedding starts through the resource-limited user service."
    exit 0
fi
entry="@reboot ${project_dir}/scripts/supervisor_start.sh"
temporary_file="$(mktemp)"
trap 'rm -f -- "${temporary_file}"' EXIT

crontab -l >"${temporary_file}" 2>/dev/null || true
if grep -Fqx -- "${entry}" "${temporary_file}"; then
    echo "LightModel Gateway supervisor autostart is already installed."
else
    printf '%s\n' "${entry}" >>"${temporary_file}"
    crontab "${temporary_file}"
    echo "LightModel Gateway supervisor autostart installed."
fi

"${project_dir}/scripts/supervisor_start.sh"
