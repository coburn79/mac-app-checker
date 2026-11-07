#!/usr/bin/env python3
"""App Update Checker - scans /Applications and checks for updates"""

__version__ = "1.0.0"

import os
import plistlib
import json
import subprocess
import time
import re

# Manual overrides: app name -> homebrew cask token
CASK_OVERRIDES = {
    'chrome': 'google-chrome',
    'google chrome': 'google-chrome',
    'whatsapp': 'whatsapp',
    'telegram': 'telegram',
    'vmware fusion': 'vmware-fusion',
    'surfshark': 'surfshark',
    'firefox': 'firefox',
    'shottr': 'shottr',
    'onenote': 'microsoft-onenote',
    'microsoft onenote': 'microsoft-onenote',
    'word': 'microsoft-word',
    'excel': 'microsoft-excel',
    'powerpoint': 'microsoft-powerpoint',
    'appcleaner': 'appcleaner',
    'vlc': 'vlc',
    'zoom': 'zoom',
    '1password': '1password',
    'dropbox': 'dropbox',
    'spotify': 'spotify',
    'slack': 'slack',
    'notion': 'notion',
    'discord': 'discord',
    'rectangle': 'rectangle',
    'alfred': 'alfred',
    'bartender': 'bartender',
    'iterm': 'iterm2',
    'iterm2': 'iterm2',
    'visual studio code': 'visual-studio-code',
    'vscode': 'visual-studio-code',
    'figma': 'figma',
    'sketch': 'sketch',
    'tower': 'tower',
    'sourcetree': 'sourcetree',
    'transmit': 'transmit',
    'cyberduck': 'cyberduck',
    'cleanmymac': 'cleanmymac',
    'cleanmymac x': 'cleanmymac',
    'dashlane': 'dashlane',
    'nordvpn': 'nordvpn',
    'expressvpn': 'expressvpn',
    'handbrake': 'handbrake',
    'imageoptim': 'imageoptim',
    'signal': 'signal',
    'skype': 'skype',
    'teamviewer': 'teamviewer',
    'keka': 'keka',
    'the unarchiver': 'the-unarchiver',
    'bettertouchtool': 'bettertouchtool',
    'mimestream': 'mimestream',
    'fantastical': 'fantastical',
    'timing': 'timing',
    'forklift': 'forklift',
    'proxyman': 'proxyman',
    'paw': 'paw',
    'postman': 'postman',
    'insomnia': 'insomnia',
    'tableplus': 'tableplus',
    'sequel pro': 'sequel-pro',
    'bbedit': 'bbedit',
    'nova': 'nova',
    'codeshot': 'codeshot',
    'screenflow': 'screenflow',
    'screenium': 'screenium',
    'capture one': 'capture-one',
    'luminar': 'luminar-ai',
}

def fetch_cask(token):
    try:
        result = subprocess.run(
            ['curl', '-s', '--max-time', '8', f'https://formulae.brew.sh/api/cask/{token}.json'],
            capture_output=True, text=True
        )
        if result.returncode == 0 and result.stdout.strip():
            data = json.loads(result.stdout)
            # Make sure it's a valid cask response (not an error)
            if 'version' in data:
                return data
    except Exception:
        pass
    return None

def fetch_mas(bundle_id):
    try:
        result = subprocess.run(
            ['curl', '-s', '--max-time', '8',
             f'https://itunes.apple.com/lookup?bundleId={bundle_id}&country=us'],
            capture_output=True, text=True
        )
        if result.returncode == 0 and result.stdout.strip():
            data = json.loads(result.stdout)
            if data.get('resultCount', 0) > 0:
                return data['results'][0].get('version')
    except Exception:
        pass
    return None

def is_app_store_app(app_path):
    return os.path.exists(os.path.join(app_path, 'Contents', '_MASReceipt', 'receipt'))

def read_app_info(app_path):
    plist_path = os.path.join(app_path, 'Contents', 'Info.plist')
    if not os.path.exists(plist_path):
        return None
    try:
        with open(plist_path, 'rb') as f:
            p = plistlib.load(f)
        return {
            'name': p.get('CFBundleName') or p.get('CFBundleDisplayName') or os.path.basename(app_path).replace('.app', ''),
            'version': p.get('CFBundleShortVersionString', 'Unknown'),
            'bundle_id': p.get('CFBundleIdentifier', ''),
            'path': app_path,
        }
    except Exception:
        return None

def find_apps():
    apps = []
    for path in ["/Applications", os.path.expanduser("~/Applications")]:
        if os.path.exists(path):
            try:
                for entry in os.scandir(path):
                    if entry.name.endswith('.app') and entry.is_dir():
                        info = read_app_info(entry.path)
                        if info:
                            apps.append(info)
            except PermissionError:
                pass
    return apps

def normalize_to_token(name):
    name_lower = name.lower()
    if name_lower in CASK_OVERRIDES:
        return CASK_OVERRIDES[name_lower]
    name = name_lower
    name = re.sub(r'[^a-z0-9\s-]', '', name)
    name = re.sub(r'\s+', '-', name.strip())
    return name

def compare_versions(v1, v2):
    """Returns True if v2 is newer than v1"""
    if v1 == v2:
        return False
    def parse(v):
        parts = []
        for x in re.split(r'[.\-,_]', str(v)):
            if x.isdigit():
                parts.append(int(x))
            else:
                parts.append(x)
        return parts
    try:
        return parse(v2) > parse(v1)
    except Exception:
        return v1 != v2

def main():
    apps = find_apps()
    results = []

    for app in apps:
        name = app['name']
        version = app['version']
        bundle_id = app['bundle_id']
        latest = None
        source = None
        cask_token = None

        if is_app_store_app(app['path']):
            latest = fetch_mas(bundle_id)
            source = 'App Store'
        else:
            token = normalize_to_token(name)
            cask = fetch_cask(token)
            if cask:
                latest = cask.get('version')
                source = 'Homebrew Cask'
                cask_token = token
            else:
                source = 'Direct'
        time.sleep(0.05)

        update_available = bool(latest and compare_versions(version, latest))

        results.append({
            'name': name,
            'installed': version,
            'latest': latest or '',
            'source': source,
            'cask_token': cask_token or '',
            'update_available': update_available,
            'found': latest is not None,
        })

    results.sort(key=lambda x: (not x['update_available'], not x['found'], x['name'].lower()))
    print(json.dumps(results, indent=2))

if __name__ == '__main__':
    main()
