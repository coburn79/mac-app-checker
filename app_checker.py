#!/usr/bin/env python3
"""Mac App Checker - scans /Applications and checks for updates"""

__version__ = "1.1.0"

import os, plistlib, json, subprocess, time, re, sys, tempfile, shutil, threading

# ── Colour helpers ────────────────────────────────────────────────────────────
USE_COLOR = sys.stdout.isatty()
def _c(code, t): return f"\033[{code}m{t}\033[0m" if USE_COLOR else t
RED = lambda t: _c("91", t)
YEL = lambda t: _c("93", t)
GRN = lambda t: _c("92", t)
GRY = lambda t: _c("90", t)
BLD = lambda t: _c("1",  t)

# ── Cask name overrides ───────────────────────────────────────────────────────
CASK_OVERRIDES = {
    'chrome': 'google-chrome', 'google chrome': 'google-chrome',
    'whatsapp': 'whatsapp', 'telegram': 'telegram', 'claude': 'claude',
    'vmware fusion': 'vmware-fusion', 'surfshark': 'surfshark',
    'firefox': 'firefox', 'shottr': 'shottr',
    'onenote': 'microsoft-onenote', 'microsoft onenote': 'microsoft-onenote',
    'word': 'microsoft-word', 'excel': 'microsoft-excel',
    'powerpoint': 'microsoft-powerpoint', 'appcleaner': 'appcleaner',
    'vlc': 'vlc', 'zoom': 'zoom', '1password': '1password',
    'dropbox': 'dropbox', 'spotify': 'spotify', 'slack': 'slack',
    'discord': 'discord', 'rectangle': 'rectangle', 'alfred': 'alfred',
    'bartender': 'bartender', 'iterm2': 'iterm2',
    'visual studio code': 'visual-studio-code', 'figma': 'figma',
    'sketch': 'sketch', 'transmit': 'transmit', 'cyberduck': 'cyberduck',
    'signal': 'signal', 'skype': 'skype', 'teamviewer': 'teamviewer',
    'keka': 'keka', 'the unarchiver': 'the-unarchiver',
    'bettertouchtool': 'bettertouchtool', 'fantastical': 'fantastical',
    'postman': 'postman', 'tableplus': 'tableplus', 'bbedit': 'bbedit',
    'nova': 'nova', 'nordvpn': 'nordvpn', 'expressvpn': 'expressvpn',
    'handbrake': 'handbrake', 'imageoptim': 'imageoptim',
}

# ── Network ───────────────────────────────────────────────────────────────────
def fetch_url(url, timeout=10):
    try:
        r = subprocess.run(
            ['curl', '-s', '--max-time', str(timeout),
             '-A', f'MacAppChecker/{__version__}', url],
            capture_output=True, text=True
        )
        if r.returncode == 0 and r.stdout.strip():
            return json.loads(r.stdout)
    except Exception:
        pass
    return None

def fetch_cask(token):
    d = fetch_url(f'https://formulae.brew.sh/api/cask/{token}.json')
    return d if d and 'version' in d else None

def fetch_mas(bundle_id):
    d = fetch_url(f'https://itunes.apple.com/lookup?bundleId={bundle_id}&country=us')
    if d and d.get('resultCount', 0) > 0:
        return d['results'][0].get('version')
    return None

# ── App scanning ──────────────────────────────────────────────────────────────
def is_app_store_app(path):
    return os.path.exists(os.path.join(path, 'Contents', '_MASReceipt', 'receipt'))

def read_app_info(path):
    plist_path = os.path.join(path, 'Contents', 'Info.plist')
    if not os.path.exists(plist_path):
        return None
    try:
        with open(plist_path, 'rb') as f:
            p = plistlib.load(f)
        return {
            'name':      p.get('CFBundleName') or p.get('CFBundleDisplayName') or os.path.basename(path).replace('.app', ''),
            'version':   p.get('CFBundleShortVersionString', 'Unknown'),
            'bundle_id': p.get('CFBundleIdentifier', ''),
            'path':      path,
        }
    except Exception:
        return None

def find_apps():
    apps = []
    for base in ['/Applications', os.path.expanduser('~/Applications')]:
        if not os.path.exists(base):
            continue
        try:
            for entry in os.scandir(base):
                if entry.name.endswith('.app') and entry.is_dir():
                    info = read_app_info(entry.path)
                    if info:
                        apps.append(info)
        except PermissionError:
            pass
    return apps

# ── Version utilities ─────────────────────────────────────────────────────────
def clean_ver(v):
    return v.split(',')[0] if v else ''

def normalize_to_token(name):
    lower = name.lower()
    if lower in CASK_OVERRIDES:
        return CASK_OVERRIDES[lower]
    s = re.sub(r'[^a-z0-9\s-]', '', lower)
    return re.sub(r'\s+', '-', s.strip())

def versions_equal(v1, v2_raw):
    v2 = clean_ver(v2_raw)
    if v1 == v2:
        return True
    norm = lambda v: re.sub(r'(\.0)+$', '', v)
    return norm(v1) == norm(v2)

def is_newer(installed, latest_raw):
    cv = clean_ver(latest_raw)
    if versions_equal(installed, latest_raw):
        return False
    def parse(v):
        return [int(x) if x.isdigit() else x for x in re.split(r'[.\-_]', v)]
    try:
        return parse(cv) > parse(installed)
    except Exception:
        return cv != installed

def get_status(app):
    if not app.get('latest'):
        return 'unknown'
    if versions_equal(app['installed'], app['latest']):
        return 'current'
    if is_newer(app['installed'], app['latest']):
        try:
            gap = int(clean_ver(app['latest']).split('.')[0]) - int(app['installed'].split('.')[0])
            return 'behind' if gap >= 2 else 'update'
        except (ValueError, IndexError):
            return 'update'
    return 'current'

# ── macOS check ───────────────────────────────────────────────────────────────
def check_macos():
    try:
        ver  = subprocess.run(['sw_vers', '-productVersion'], capture_output=True, text=True).stdout.strip()
        name = subprocess.run(['sw_vers', '-productName'],    capture_output=True, text=True).stdout.strip() or 'macOS'
        su   = subprocess.run(['softwareupdate', '--list'],   capture_output=True, text=True, timeout=40)
        output = su.stdout + su.stderr
        updates = [
            line.lstrip('*- ').strip()
            for line in output.split('\n')
            if line.strip().startswith(('*', '-')) and line.strip()
        ]
        return {'name': name, 'version': ver, 'updates': updates, 'has_update': bool(updates)}
    except Exception:
        try:
            ver = subprocess.run(['sw_vers', '-productVersion'], capture_output=True, text=True).stdout.strip()
        except Exception:
            ver = 'Unknown'
        return {'name': 'macOS', 'version': ver, 'updates': [], 'has_update': False}

# ── Installer helpers ─────────────────────────────────────────────────────────
def _quit_app(name):
    subprocess.run(['osascript', '-e', f'tell application "{name}" to quit'],
                   capture_output=True, timeout=5)
    time.sleep(1)

def _install_dmg(url, app_name):
    print("  Downloading...", flush=True)
    with tempfile.NamedTemporaryFile(suffix='.dmg', delete=False) as f:
        tmp = f.name
    try:
        r = subprocess.run(['curl', '-L', '--progress-bar', '-o', tmp, url], timeout=600)
        if r.returncode != 0:
            print(GRY("  Download failed.")); return False
        print("  Mounting disk image...", flush=True)
        mr = subprocess.run(['hdiutil', 'attach', '-nobrowse', '-noverify', tmp],
                            capture_output=True, text=True)
        if mr.returncode != 0:
            print(GRY("  Mount failed.")); return False
        mount_point = next(
            ('/Volumes/' + line.split('/Volumes/', 1)[1].strip()
             for line in mr.stdout.split('\n') if '/Volumes/' in line),
            None
        )
        if not mount_point:
            print(GRY("  Could not find mount point.")); return False
        try:
            app_in_dmg = next(
                (os.path.join(mount_point, e) for e in os.listdir(mount_point) if e.endswith('.app')),
                None
            )
            if not app_in_dmg:
                print(GRY("  No .app found in DMG.")); return False
            dest = f'/Applications/{os.path.basename(app_in_dmg)}'
            print(f"  Installing {os.path.basename(app_in_dmg)}...", flush=True)
            _quit_app(app_name)
            if os.path.exists(dest):
                try:
                    shutil.rmtree(dest)
                except PermissionError:
                    print(GRY("  Permission denied — try: sudo python3 app_checker.py"))
                    return False
            shutil.copytree(app_in_dmg, dest, symlinks=True)
            print(GRN(f"  ✓ Installed to {dest}"))
            return True
        finally:
            subprocess.run(['hdiutil', 'detach', mount_point, '-quiet'], capture_output=True)
    except Exception as e:
        print(GRY(f"  Error: {e}")); return False
    finally:
        try: os.unlink(tmp)
        except: pass

def _install_zip(url, app_name):
    print("  Downloading...", flush=True)
    with tempfile.TemporaryDirectory() as tmp_dir:
        zip_path = os.path.join(tmp_dir, 'app.zip')
        r = subprocess.run(['curl', '-L', '--progress-bar', '-o', zip_path, url], timeout=600)
        if r.returncode != 0:
            print(GRY("  Download failed.")); return False
        extract_dir = os.path.join(tmp_dir, 'extracted')
        os.makedirs(extract_dir)
        subprocess.run(['unzip', '-q', zip_path, '-d', extract_dir])
        app_in_zip = None
        for root, dirs, _ in os.walk(extract_dir):
            for d in dirs:
                if d.endswith('.app'):
                    app_in_zip = os.path.join(root, d); break
            if app_in_zip: break
        if not app_in_zip:
            print(GRY("  No .app found in ZIP.")); return False
        dest = f'/Applications/{os.path.basename(app_in_zip)}'
        print(f"  Installing {os.path.basename(app_in_zip)}...", flush=True)
        _quit_app(app_name)
        if os.path.exists(dest):
            try:
                shutil.rmtree(dest)
            except PermissionError:
                print(GRY("  Permission denied — try: sudo python3 app_checker.py"))
                return False
        shutil.copytree(app_in_zip, dest, symlinks=True)
        print(GRN(f"  ✓ Installed to {dest}"))
        return True

def do_update(app):
    """Update a single app — dispatches by source type."""
    if app['source'] == 'App Store':
        print("  Opening App Store updates page...")
        subprocess.run(['open', 'macappstore://showUpdatesPage'])
        return True

    token = app.get('cask_token')
    if not token:
        print(f"  Launching {app['name']} to trigger its built-in updater...")
        subprocess.run(['open', '-a', app['name']])
        return True

    print("  Fetching download info...", flush=True)
    cask = fetch_cask(token)
    if not cask:
        print(GRY("  Could not get download info.")); return False

    url      = cask.get('url', '')
    homepage = cask.get('homepage', '')
    ext      = url.lower().split('?')[0].split('#')[0]

    if ext.endswith('.dmg'):
        return _install_dmg(url, app['name'])
    elif ext.endswith('.zip'):
        return _install_zip(url, app['name'])
    elif ext.endswith('.pkg'):
        print("  PKG installer — opening download page in browser...")
        subprocess.run(['open', homepage or url])
        return False
    else:
        target = homepage or url
        if target:
            print(f"  Opening download page for {app['name']}...")
            subprocess.run(['open', target])
        else:
            subprocess.run(['open', '-a', app['name']])
        return False

# ── Main ──────────────────────────────────────────────────────────────────────
STATUS_LABEL = {
    'update':  YEL('Update available'),
    'behind':  RED('Far behind'),
    'current': GRN('Current'),
    'unknown': GRY('Unknown'),
}

def main():
    json_mode = '--json' in sys.argv

    if not json_mode:
        print(BLD(f"\nMac App Checker  v{__version__}"))
        print(GRY("Checking macOS and scanning /Applications…\n"))

    # Run macOS check concurrently with app scan
    macos_result = {}
    t = threading.Thread(target=lambda: macos_result.update(check_macos()), daemon=True)
    t.start()

    # Scan and check apps
    apps = find_apps()
    results = []
    for app in apps:
        latest = cask_token = None
        if is_app_store_app(app['path']):
            latest = fetch_mas(app['bundle_id'])
            source = 'App Store'
        else:
            token = normalize_to_token(app['name'])
            cask  = fetch_cask(token)
            if cask:
                latest     = cask.get('version')
                source     = 'Homebrew Cask'
                cask_token = token
            else:
                source = 'Direct'
        time.sleep(0.05)
        results.append({**app, 'latest': latest or '', 'source': source, 'cask_token': cask_token or ''})

    t.join(timeout=40)

    for r in results:
        r['status'] = get_status(r)

    # ── JSON output mode (for dashboard) ─────────────────────────────────────
    if json_mode:
        print(json.dumps({'macos': macos_result, 'apps': results}, indent=2))
        return

    # ── macOS status line ─────────────────────────────────────────────────────
    m = macos_result
    if m.get('has_update'):
        upd_names = ', '.join(m['updates'][:2]) + (' …' if len(m['updates']) > 2 else '')
        print(f"macOS  {YEL(m.get('version','?'))}  {YEL('Update available:')} {upd_names}")
    elif m.get('version'):
        print(f"macOS  {GRN(m['version'])}  {GRN('Up to date')}")
    else:
        print(f"macOS  {GRY('Could not check')}")

    # ── App categories ────────────────────────────────────────────────────────
    needs  = [r for r in results if r['status'] in ('update', 'behind')]
    ok     = [r for r in results if r['status'] == 'current']
    unk    = [r for r in results if r['status'] == 'unknown']

    print(f"\nScanned {len(results)} apps — "
          f"{YEL(str(len(needs)))} need updates · "
          f"{GRN(str(len(ok)))} up to date · "
          f"{GRY(str(len(unk)))} unknown\n")

    if not needs:
        print(GRN("All apps are up to date!"))
        return

    # ── Updates table ─────────────────────────────────────────────────────────
    W = 70
    print(BLD("UPDATES AVAILABLE"))
    print(GRY("─" * W))
    print(f"  {'#':<4}{'App':<28}{'Installed':<16}{'Latest'}")
    print(GRY("─" * W))
    for i, app in enumerate(needs, 1):
        cv = clean_ver(app.get('latest', '')) or '—'
        status = STATUS_LABEL[app['status']]
        print(f"  {i:<4}{app['name']:<28}{app['installed']:<16}{cv:<16}  {status}")
    print(GRY("─" * W))

    if ok:
        names = ', '.join(r['name'] for r in ok[:7]) + (f' +{len(ok)-7} more' if len(ok) > 7 else '')
        print(f"\n{GRN('UP TO DATE:')} {GRY(names)}")
    if unk:
        names = ', '.join(r['name'] for r in unk[:5]) + (f' +{len(unk)-5} more' if len(unk) > 5 else '')
        print(f"{GRY('UNKNOWN:')} {GRY(names)}")

    # ── Update prompt ─────────────────────────────────────────────────────────
    print(f"\n{BLD('What would you like to update?')}")
    print(f"  Enter a number {GRY(f'(1–{len(needs)})')}, {YEL('a')} for all, or press {GRY('Enter')} to exit: ", end='', flush=True)

    try:
        choice = input().strip().lower()
    except (EOFError, KeyboardInterrupt):
        print(); return

    if not choice or choice == 'q':
        return

    to_update = []
    if choice == 'a':
        to_update = needs
    else:
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(needs):
                to_update = [needs[idx]]
            else:
                print(GRY(f"  Please enter a number between 1 and {len(needs)}.")); return
        except ValueError:
            print(GRY("  Invalid input.")); return

    for app in to_update:
        cv = clean_ver(app.get('latest', ''))
        print(f"\n{BLD('Updating')} {app['name']}  {GRY(app['installed'])} → {GRN(cv)}")
        do_update(app)

    print(f"\n{GRN('Done.')}\n")

if __name__ == '__main__':
    main()
