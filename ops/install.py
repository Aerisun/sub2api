"""Render project-local service units and register links in user systemd."""
import argparse
import json
import os
from pathlib import Path
import pwd
import grp
import subprocess

from ops.admin_api import ROOT


def install():
    manifest = json.loads((ROOT/'deployment/site.json').read_text())
    original = manifest['installed_root']
    # Rebase only deployment paths; model names, controls and prices are untouched.
    for directory in ('config', 'egress', 'ssh'):
        for path in (ROOT/'deployment/private'/directory).rglob('*'):
            if not path.is_file() or path.stat().st_size > 1024*1024:
                continue
            try:
                text = path.read_text()
            except UnicodeDecodeError:
                continue
            if original in text:
                path.write_text(text.replace(original, str(ROOT)))
                path.chmod(0o600)
    generated = ROOT/'deployment/generated/systemd'
    destination = Path.home()/'.config/systemd/user'
    destination.mkdir(parents=True, exist_ok=True)
    replacements={'@ROOT@':str(ROOT), '@HOME@':str(Path.home()),
        '@USER@':pwd.getpwuid(os.getuid()).pw_name, '@GROUP@':grp.getgrgid(os.getgid()).gr_name}
    def render(text):
        for key,value in replacements.items():text=text.replace(key,value)
        return text
    rotation=ROOT/'deployment/generated/logrotate.conf'
    rotation.parent.mkdir(parents=True,exist_ok=True)
    rotation.write_text(render((ROOT/'deployment/logrotate.conf').read_text()))
    count = 0
    for source in (ROOT/'systemd').rglob('*'):
        if not source.is_file() or source.suffix not in ('.service', '.timer', '.conf'):
            continue
        relative = source.relative_to(ROOT/'systemd')
        target = generated/relative
        target.parent.mkdir(parents=True, exist_ok=True)
        content = render(source.read_text())
        target.write_text(content)
        link = destination/relative
        link.parent.mkdir(parents=True, exist_ok=True)
        pending = link.with_name(link.name+'.sub2api-new')
        pending.unlink(missing_ok=True);pending.symlink_to(target);os.replace(pending, link)
        count += 1
    subprocess.run(['systemctl', '--user', 'daemon-reload'], check=True)
    print(json.dumps({'service_files': count, 'installed_root': str(ROOT),
                      'registration': 'symlinks only; all definitions remain inside the project'}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    install()
