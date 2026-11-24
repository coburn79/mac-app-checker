#!/usr/bin/env python3
"""Mac App Checker - scans /Applications and checks for updates"""

__version__ = "2.3.3"

import os, plistlib, json, subprocess, time, re, sys, tempfile, threading, signal, shlex
import logging
import concurrent.futures
import xml.etree.ElementTree as ET

# ── Logging ───────────────────────────────────────────────────────────────────
_LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'app_checker.log')
logging.basicConfig(
    filename=_LOG_PATH,
    level=logging.INFO,
    format='%(asctime)s  %(levelname)-7s  %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
LOG = logging.getLogger('mac_app_checker')

# ── Colour helpers (terminal mode only) ──────────────────────────────────────
USE_COLOR = sys.stdout.isatty()
def _c(code, t): return f"\033[{code}m{t}\033[0m" if USE_COLOR else t
RED = lambda t: _c("91", t)
YEL = lambda t: _c("93", t)
GRN = lambda t: _c("92", t)
GRY = lambda t: _c("90", t)
BLD = lambda t: _c("1",  t)

def kill_old_instances():
    """Kill any other running copies of this exact script."""
    my_pid = os.getpid()
    script  = os.path.abspath(__file__)
    try:
        r = subprocess.run(['pgrep', '-f', script], capture_output=True, text=True)
        for pid_str in r.stdout.strip().split():
            try:
                pid = int(pid_str)
                if pid != my_pid:
                    os.kill(pid, signal.SIGTERM)
                    LOG.info(f'Killed old instance PID {pid}')
            except (ValueError, ProcessLookupError, PermissionError):
                pass
    except Exception:
        pass

def safe_print(*args, **kwargs):
    try:
        print(*args, **kwargs)
    except (BrokenPipeError, IOError, OSError):
        pass

def open_dashboard_browser(url):
    """Open url in Chrome/Firefox if available (they don't warn about HTTP localhost)."""
    for name in ['Google Chrome', 'Firefox', 'Arc', 'Brave Browser']:
        if os.path.exists(f'/Applications/{name}.app'):
            subprocess.run(['open', '-a', name, url], capture_output=True)
            return
    import webbrowser
    webbrowser.open(url)

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

MICROSOFT_CASKS = {
    'microsoft-word', 'microsoft-excel', 'microsoft-powerpoint',
    'microsoft-onenote', 'microsoft-outlook', 'microsoft-teams', 'microsoft-edge',
}
MICROSOFT_AUTOUPDATE = '/Library/Application Support/Microsoft/MAU2.0/Microsoft AutoUpdate.app'
MICROSOFT_BUNDLE_PREFIX = 'com.microsoft.'

APPLE_SYSTEM_BUNDLES = {
    'com.apple.Safari', 'com.apple.iWork.Keynote', 'com.apple.iWork.Numbers',
    'com.apple.iWork.Pages', 'com.apple.mail', 'com.apple.Photos',
}

HIDE_BUNDLE_IDS = {''}
HIDE_APP_NAMES  = {'Mac App Checker', 'Microsoft AutoUpdate'}

HOMEPAGE_OVERRIDES = {
    'vmware fusion':          'https://www.vmware.com/products/fusion.html',
    'sky go':                 'https://www.sky.com/shop/tv/sky-go',
    'photos for google photos': 'https://apps.apple.com/app/id962194608',
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

def fetch_url_text(url, timeout=10):
    """Fetch a URL and return raw text (for XML/HTML feeds)."""
    try:
        r = subprocess.run(
            ['curl', '-s', '--max-time', str(timeout), '-L',
             '-A', f'MacAppChecker/{__version__}', url],
            capture_output=True, text=True
        )
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout
    except Exception:
        pass
    return None

def fetch_cask(token):
    d = fetch_url(f'https://formulae.brew.sh/api/cask/{token}.json')
    return d if d and 'version' in d else None

def fetch_mas(bundle_id):
    """Returns (version, apple_id_str) or (None, None)."""
    for country in ('us', 'gb'):
        d = fetch_url(f'https://itunes.apple.com/lookup?bundleId={bundle_id}&country={country}')
        if d and d.get('resultCount', 0) > 0:
            r = d['results'][0]
            return r.get('version'), str(r.get('trackId', ''))
    return None, None

def find_mas():
    for p in ('/opt/homebrew/bin/mas', '/usr/local/bin/mas'):
        if os.path.exists(p):
            return p
    return None

# ── Sparkle ───────────────────────────────────────────────────────────────────
_SPARKLE_NS = {
    'sparkle': 'http://www.andymatuschak.org/xml-namespaces/sparkle',
    'dc':      'http://purl.org/dc/elements/1.1/',
}

def get_sparkle_feed_url(app_path):
    """Return the SUFeedURL from an app's Info.plist, or None."""
    plist_path = os.path.join(app_path, 'Contents', 'Info.plist')
    try:
        with open(plist_path, 'rb') as f:
            p = plistlib.load(f)
        return p.get('SUFeedURL') or p.get('SUFeedURL ')
    except Exception:
        return None

def fetch_sparkle(feed_url, timeout=8):
    """Fetch a Sparkle appcast and return (version, changelog_html) or (None, None)."""
    text = fetch_url_text(feed_url, timeout=timeout)
    if not text:
        return None, None
    try:
        root = ET.fromstring(text)
        channel = root.find('channel')
        if channel is None:
            return None, None
        items = channel.findall('item')
        best_ver = None
        best_notes = None
        for item in items:
            NS = 'http://www.andymatuschak.org/xml-namespaces/sparkle'
            enc = item.find('enclosure')
            ver = None
            # Prefer shortVersionString (human-readable) over version (build number)
            # Check item child elements first (most apps), then enclosure attributes
            for tag in (f'{{{NS}}}shortVersionString', f'{{{NS}}}version'):
                sv = item.find(tag)
                if sv is not None and sv.text:
                    ver = sv.text.strip()
                    break
            if not ver and enc is not None:
                ver = enc.get(f'{{{NS}}}shortVersionString') or enc.get(f'{{{NS}}}version')
            if not ver:
                # Last resort: try title if it looks like a version
                title = (item.findtext('title') or '').strip()
                if re.match(r'^\d+[\.\d]+$', title):
                    ver = title
            if not ver:
                continue
            if best_ver is None or is_newer(best_ver, ver):
                best_ver = ver
                # Try release notes
                rn = item.find('{http://www.andymatuschak.org/xml-namespaces/sparkle}releaseNotesLink')
                if rn is not None and rn.text:
                    notes_text = fetch_url_text(rn.text.strip(), timeout=5)
                    best_notes = notes_text[:2000] if notes_text else None
                else:
                    desc = item.find('description')
                    if desc is not None:
                        best_notes = (desc.text or '')[:2000]
        return best_ver, best_notes
    except Exception:
        return None, None

# ── Vendor-managed apps ───────────────────────────────────────────────────────
# Apps that have their own mandatory updaters — we can detect they're outdated
# via the Homebrew cask or App Store, but cannot silently install them.
VENDOR_MANAGED_BUNDLES = {
    # Adobe
    'com.adobe.Creative-Cloud', 'com.adobe.acc.AdobeCreativeCloud',
    'com.adobe.illustrator', 'com.adobe.Photoshop', 'com.adobe.InDesign',
    'com.adobe.Premiere-Pro', 'com.adobe.after-effects', 'com.adobe.Lightroom',
    'com.adobe.LightroomClassicCC', 'com.adobe.bridge', 'com.adobe.dreamweaver',
    # Browsers (self-updating)
    'org.mozilla.firefox', 'com.google.Chrome', 'com.microsoft.edgemac',
    'com.operasoftware.Opera', 'com.brave.Browser',
    # Other self-managed
    'com.getdropbox.dropbox', 'us.zoom.xos', 'com.teamviewer.TeamViewer',
}

VENDOR_MANAGED_NAME_FRAGMENTS = {
    'adobe', 'creative cloud',
}

def is_vendor_managed(bundle_id, name):
    if bundle_id in VENDOR_MANAGED_BUNDLES:
        return True
    name_l = name.lower()
    return any(frag in name_l for frag in VENDOR_MANAGED_NAME_FRAGMENTS)

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
            'name':          p.get('CFBundleName') or p.get('CFBundleDisplayName') or os.path.basename(path).replace('.app', ''),
            'version':       p.get('CFBundleShortVersionString', 'Unknown'),
            'build_version': p.get('CFBundleVersion', ''),
            'bundle_id':     p.get('CFBundleIdentifier', ''),
            'path':          path,
            'sparkle_url':   p.get('SUFeedURL', ''),
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

def _parse_ver(v):
    """Parse a version string into a comparable list.

    Pre-release labels (alpha, beta, rc) sort before a stable release,
    so '1.2.3' > '1.2.3-rc1' > '1.2.3-beta' > '1.2.3-alpha'.
    A trailing 1 for stable vs 0 for pre-release acts as the tiebreaker.
    """
    parts = []
    has_prerelease = False
    for x in re.split(r'[.\-_]', v):
        if not x:
            continue
        if x.isdigit():
            parts.append(int(x))
        else:
            xl = x.lower().rstrip('0123456789')
            order = {'alpha': -4, 'a': -4, 'beta': -3, 'b': -3, 'pre': -3, 'rc': -2}
            if xl in order:
                has_prerelease = True
                n = int(x[len(xl):]) if x[len(xl):].isdigit() else 0
                parts.append(order[xl] * 1000 + n)
            else:
                parts.append(0)
    parts.append(0 if has_prerelease else 1)
    return parts

def is_prerelease(v):
    s = clean_ver(v).lower()
    return bool(re.search(r'(alpha|beta|\.pre|[-_]rc|[-_]pre|b\d*$|a\d*$)', s))

def is_newer(installed, latest_raw):
    cv = clean_ver(latest_raw)
    if versions_equal(installed, latest_raw):
        return False
    if is_prerelease(cv) and not is_prerelease(installed):
        return False
    try:
        return _parse_ver(cv) > _parse_ver(installed)
    except Exception:
        return cv != installed

def get_status(app):
    if app.get('source') == 'Apple':
        return 'system'
    if not app.get('latest'):
        return 'unknown'
    if versions_equal(app['installed'], app['latest']):
        return 'current'
    if is_newer(app['installed'], app['latest']):
        if app.get('vendor_managed'):
            return 'vendor'
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

def _backup_app(dest, app_name):
    """Copy existing .app to ~/Downloads/AppBackups/ before overwriting. Best-effort."""
    if not os.path.exists(dest):
        return
    try:
        backup_dir = os.path.expanduser('~/Downloads/AppBackups')
        os.makedirs(backup_dir, exist_ok=True)
        # Get current installed version for the backup name
        plist_path = os.path.join(dest, 'Contents', 'Info.plist')
        ver = ''
        try:
            with open(plist_path, 'rb') as f:
                p = plistlib.load(f)
            ver = p.get('CFBundleShortVersionString', '')
        except Exception:
            pass
        bname = re.sub(r'[^a-zA-Z0-9._-]', '_', app_name)
        suffix = f'_{ver}' if ver else ''
        backup_path = os.path.join(backup_dir, f'{bname}{suffix}.app')
        subprocess.run(['ditto', dest, backup_path], capture_output=True)
        LOG.info(f'Backed up {app_name} v{ver} to {backup_path}')
    except Exception as e:
        LOG.warning(f'Backup failed for {app_name}: {e}')

def _ditto_install(src_app, app_name):
    """Copy src_app into /Applications using ditto (preserves macOS metadata)."""
    dest = f'/Applications/{os.path.basename(src_app)}'
    _quit_app(app_name)
    _backup_app(dest, app_name)
    if os.path.exists(dest):
        r = subprocess.run(['rm', '-rf', dest], capture_output=True)
        if r.returncode != 0:
            return False, 'Permission denied removing old version'
    r = subprocess.run(['ditto', src_app, dest], capture_output=True, text=True)
    if r.returncode != 0:
        return False, r.stderr.strip() or 'Install failed (permission denied?)'
    subprocess.run(['xattr', '-cr', dest], capture_output=True)
    LOG.info(f'Installed {os.path.basename(src_app)} to {dest}')
    return True, f'Installed {os.path.basename(src_app)}'

def _install_from_url(url, app_name):
    """Download url, detect DMG/ZIP/PKG, install. Returns (success, message)."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        dl = os.path.join(tmp_dir, 'download')
        LOG.info(f'Downloading {app_name} from {url}')
        safe_print(f'  Downloading {app_name} (large files may take a few minutes)...')
        r = subprocess.run(
            ['curl', '-L', '-s', '-o', dl, '--retry', '2', '--max-time', '1800', url],
            timeout=1850
        )
        if r.returncode != 0 or not os.path.exists(dl):
            LOG.warning(f'Download failed for {app_name}')
            return False, 'Download failed'

        # Detect file type from magic bytes, not URL extension
        ft = subprocess.run(['file', dl], capture_output=True, text=True).stdout.lower()
        u  = url.lower().split('?')[0]

        is_dmg = any(k in ft for k in ('apple disk image', 'udif', 'binhex')) or u.endswith('.dmg')
        is_zip = any(k in ft for k in ('zip archive', 'jar')) or u.endswith('.zip')
        is_pkg = any(k in ft for k in ('xar archive', 'package installer')) or u.endswith('.pkg')

        LOG.info(f'{app_name}: file type detected — dmg={is_dmg} zip={is_zip} pkg={is_pkg}  ({ft.strip()[:80]})')

        if is_dmg:
            safe_print(f'  Mounting disk image...')
            mr = subprocess.run(
                ['hdiutil', 'attach', '-nobrowse', '-noverify', dl],
                capture_output=True, text=True
            )
            if mr.returncode != 0:
                LOG.warning(f'Could not mount DMG for {app_name}: {mr.stderr.strip()}')
                return False, 'Could not mount disk image'
            mount_point = next(
                ('/Volumes/' + line.split('/Volumes/', 1)[1].strip()
                 for line in mr.stdout.split('\n') if '/Volumes/' in line),
                None
            )
            if not mount_point:
                return False, 'Could not find mount point'
            try:
                app_src = next(
                    (os.path.join(mount_point, e)
                     for e in os.listdir(mount_point) if e.endswith('.app')),
                    None
                )
                if not app_src:
                    return False, 'No .app found in disk image'
                safe_print(f'  Installing {os.path.basename(app_src)}...')
                return _ditto_install(app_src, app_name)
            except Exception as e:
                LOG.error(f'DMG install error for {app_name}: {e}')
                return False, str(e)
            finally:
                subprocess.run(['hdiutil', 'detach', mount_point, '-quiet'], capture_output=True)

        elif is_zip:
            extract_dir = os.path.join(tmp_dir, 'x')
            os.makedirs(extract_dir)
            subprocess.run(['unzip', '-q', dl, '-d', extract_dir], capture_output=True)
            app_src = None
            for root, dirs, _ in os.walk(extract_dir):
                for d in dirs:
                    if d.endswith('.app'):
                        app_src = os.path.join(root, d)
                        break
                if app_src:
                    break
            if not app_src:
                return False, 'No .app found in ZIP'
            safe_print(f'  Installing {os.path.basename(app_src)}...')
            return _ditto_install(app_src, app_name)

        elif is_pkg:
            pkg_tmp = dl + '.pkg'
            os.rename(dl, pkg_tmp)
            safe_print(f'  Installing {app_name} (you may see a password prompt)...')
            LOG.info(f'Running silent pkg install for {app_name}')
            # Silent install via macOS installer command — one password dialog, no wizard
            script = f'do shell script "installer -pkg {shlex.quote(pkg_tmp)} -target /" with administrator privileges'
            r = subprocess.run(['osascript', '-e', script],
                               capture_output=True, text=True, timeout=600)
            if r.returncode == 0:
                LOG.info(f'Silent pkg install succeeded for {app_name}')
                return True, f'{app_name} installed successfully'
            else:
                err = r.stderr.strip() or r.stdout.strip()
                LOG.warning(f'Silent install failed for {app_name}: {err}')
                # Fallback: save to Downloads and open wizard
                pkg_name = re.sub(r'[^a-zA-Z0-9._-]', '_', app_name) + '.pkg'
                pkg_dest = os.path.join(os.path.expanduser('~/Downloads'), pkg_name)
                subprocess.run(['cp', pkg_tmp, pkg_dest], capture_output=True)
                subprocess.run(['open', pkg_dest])
                return True, f'Saved to ~/Downloads/{pkg_name} — follow the installer prompts'

        else:
            LOG.warning(f'Unknown file format for {app_name}: {ft.strip()[:100]}')
            return False, 'Unknown file format (not DMG, ZIP, or PKG)'

# ── Update dispatcher ─────────────────────────────────────────────────────────
def do_update(app):
    """Trigger an update for the app. Returns (success, message, info=False)."""
    token = app.get('cask_token', '')
    LOG.info(f'Update requested: {app["name"]} (token={token!r}, source={app["source"]})')

    if app.get('mas_app') or app['source'] in ('App Store', 'Apple'):
        apple_id = app.get('apple_id', '')
        mas_bin  = find_mas()
        if mas_bin and apple_id:
            safe_print(f'  Installing {app["name"]} via mas (silent)...')
            r = subprocess.run([mas_bin, 'upgrade', apple_id],
                               capture_output=True, text=True, timeout=600)
            if r.returncode == 0:
                LOG.info(f'mas upgrade succeeded for {app["name"]}')
                return True, f'{app["name"]} updated successfully', False
            LOG.warning(f'mas upgrade failed for {app["name"]}: {r.stderr.strip()}')
        # mas not available — explain clearly, do NOT auto-open App Store
        if not mas_bin:
            msg = ('Mac App Store app — open App Store > Updates tab to update manually. '
                   'For silent one-click updates, install Homebrew (brew.sh) then run: brew install mas')
        else:
            msg = f'mas could not update {app["name"]} — open App Store > Updates tab manually'
        LOG.info(f'App Store fallback for {app["name"]}: mas_bin={mas_bin!r}')
        return False, msg, True  # info=True → amber state, nothing opened

    if app.get('vendor_managed'):
        # Vendor-managed apps are handled client-side (direct link), not via do_update
        return False, (f'{app["name"]} uses its own built-in updater — '
                       'open the app and use Help > Check for Updates'), True

    if not token:
        safe_print(f'  Opening {app["name"]} to self-update...')
        subprocess.run(['open', '-a', app['name']])
        return True, 'App opened — check for updates inside the app'

    safe_print(f'  Fetching download info for {app["name"]}...')
    cask = fetch_cask(token)
    if not cask:
        LOG.warning(f'Could not fetch cask for {token}')
        return False, 'Could not fetch download info'

    url = cask.get('url', '')
    if not url:
        homepage = cask.get('homepage', '')
        if homepage:
            subprocess.run(['open', homepage])
            return True, 'Download page opened'
        return False, 'No download URL found'

    return _install_from_url(url, app['name'])

# ── HTML: scanning progress page ─────────────────────────────────────────────
SCANNING_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Mac App Checker — Scanning…</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap">
<style>
:root{--bg:#f0f0f5;--surface:#fff;--border:#d8d8e0;--fg:#1a1a2e;--fg2:#6b6b80;--accent:#0057d8;--radius:12px}
@media(prefers-color-scheme:dark){:root{--bg:#111117;--surface:#1e1e26;--border:#2e2e3a;--fg:#f0f0f8;--fg2:#9090a8;--accent:#4d8bff;color-scheme:dark}}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Inter',-apple-system,sans-serif;background:var(--bg);color:var(--fg);min-height:100vh;display:flex;flex-direction:column}
.topbar{background:var(--surface);border-bottom:1px solid var(--border);padding:14px 24px}
.brand{font-size:17px;font-weight:700;letter-spacing:-.025em}
.main{flex:1;display:flex;align-items:center;justify-content:center;padding:40px 24px}
.card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:36px 40px;max-width:400px;width:100%;text-align:center}
.spin{width:44px;height:44px;border:3px solid var(--border);border-top-color:var(--accent);border-radius:50%;animation:spin .85s linear infinite;margin:0 auto 22px}
@keyframes spin{to{transform:rotate(360deg)}}
.card-title{font-size:18px;font-weight:700;margin-bottom:8px}
.card-sub{font-size:13px;color:var(--fg2);margin-bottom:24px;min-height:18px}
.bar-bg{height:6px;background:var(--border);border-radius:99px;overflow:hidden;margin-bottom:14px}
.bar-fill{height:100%;background:var(--accent);border-radius:99px;transition:width .5s ease;width:0%}
.detail{font-size:12px;color:var(--fg2);font-family:monospace;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-height:16px}
</style>
</head>
<body>
<div class="topbar"><span class="brand">Mac App Checker</span></div>
<div class="main">
  <div class="card">
    <div class="spin"></div>
    <div class="card-title">Scanning your apps…</div>
    <div class="card-sub" id="sub">Looking for installed apps</div>
    <div class="bar-bg"><div class="bar-fill" id="bar"></div></div>
    <div class="detail" id="detail">Starting…</div>
  </div>
</div>
<script>
async function poll(){
  try{
    const d=await(await fetch('/api/status')).json();
    if(!d.scanning){location.reload();return}
    if(d.total>0){
      document.getElementById('bar').style.width=Math.round(d.done/d.total*100)+'%';
      document.getElementById('sub').textContent=d.done+' of '+d.total+' apps checked';
    }
    document.getElementById('detail').textContent=d.app||'Starting…';
  }catch(e){}
  setTimeout(poll,800);
}
poll();
</script>
</body></html>"""

# ── HTML: results dashboard ───────────────────────────────────────────────────
HTML_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Mac App Checker</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap">
<style>
:root{
  --bg:#f0f0f5;--surface:#ffffff;--surface2:#f5f5fa;
  --border:#d8d8e0;--border2:#ebebf0;
  --fg:#1a1a2e;--fg2:#6b6b80;--fg3:#9898a8;
  --accent:#0057d8;--accent-bg:#e8f0ff;
  --ok-fg:#1a7f4b;--ok-bg:#e8f8ef;
  --upd-fg:#8a4a00;--upd-bg:#fff3e0;
  --crit-fg:#b91c1c;--crit-bg:#fff1f1;
  --unk-fg:#5c5c70;--unk-bg:#ebebf2;
  --radius:12px;
}
@media(prefers-color-scheme:dark){
  :root:not([data-theme="light"]){
    --bg:#111117;--surface:#1e1e26;--surface2:#26262f;
    --border:#2e2e3a;--border2:#252530;
    --fg:#f0f0f8;--fg2:#9090a8;--fg3:#5a5a70;
    --accent:#4d8bff;--accent-bg:#0d2040;
    --ok-fg:#34d399;--ok-bg:#052e1a;
    --upd-fg:#fbbf24;--upd-bg:#2c1a00;
    --crit-fg:#f87171;--crit-bg:#2d0808;
    --unk-fg:#8080a0;--unk-bg:#1e1e28;
    color-scheme:dark;
  }
}
:root[data-theme="dark"]{
  --bg:#111117;--surface:#1e1e26;--surface2:#26262f;
  --border:#2e2e3a;--border2:#252530;
  --fg:#f0f0f8;--fg2:#9090a8;--fg3:#5a5a70;
  --accent:#4d8bff;--accent-bg:#0d2040;
  --ok-fg:#34d399;--ok-bg:#052e1a;
  --upd-fg:#fbbf24;--upd-bg:#2c1a00;
  --crit-fg:#f87171;--crit-bg:#2d0808;
  --unk-fg:#8080a0;--unk-bg:#1e1e28;
  color-scheme:dark;
}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Inter',-apple-system,BlinkMacSystemFont,sans-serif;background:var(--bg);color:var(--fg);font-size:14px;line-height:1.5;min-height:100vh}
.topbar{position:sticky;top:0;z-index:20;background:var(--surface);border-bottom:1px solid var(--border);padding:14px 24px;display:flex;align-items:center;justify-content:space-between;gap:12px}
.brand{display:flex;flex-direction:column;gap:1px}
.brand-name{font-size:17px;font-weight:700;letter-spacing:-.025em;color:var(--fg)}
.brand-sub{font-size:11px;color:var(--fg2)}
.btn-quit{padding:6px 14px;border-radius:8px;border:1px solid var(--border);background:transparent;color:var(--fg2);font-family:inherit;font-size:12px;font-weight:500;cursor:pointer;transition:all .15s;white-space:nowrap}
.btn-quit:hover{border-color:var(--crit-fg);color:var(--crit-fg)}
.modal-backdrop{display:none;position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:999;align-items:center;justify-content:center}
.modal-backdrop.open{display:flex}
.modal{background:var(--surface);border-radius:18px;padding:28px 28px 24px;max-width:340px;width:calc(100% - 48px);text-align:center;box-shadow:0 20px 60px rgba(0,0,0,.35)}
.modal-icon{font-size:34px;margin-bottom:10px}
.modal-title{font-size:17px;font-weight:700;margin-bottom:6px}
.modal-desc{font-size:13px;color:var(--fg2);margin-bottom:22px;line-height:1.5}
.modal-btns{display:flex;gap:10px;justify-content:center}
.modal-btn{padding:9px 22px;border-radius:10px;font-family:inherit;font-size:14px;font-weight:600;cursor:pointer;transition:opacity .15s}
.modal-cancel{border:1px solid var(--border);background:transparent;color:var(--fg)}
.modal-cancel:hover{background:var(--surface2)}
.modal-stop{border:none;background:var(--crit-fg);color:#fff}
.modal-stop:hover{opacity:.88}
.main{max-width:860px;margin:0 auto;padding:22px 24px 56px}
.macos-card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:13px 16px;display:flex;align-items:center;gap:12px;margin-bottom:18px;font-size:13px}
.macos-icon{font-size:22px;flex-shrink:0;line-height:1}
.macos-info{display:flex;flex-direction:column;gap:1px}
.macos-name{font-weight:600;font-size:13px}
.macos-status{font-size:12px;color:var(--fg2)}
.macos-status.ok{color:var(--ok-fg)}
.macos-status.upd{color:var(--upd-fg);font-weight:500}
.filters{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:18px}
@media(max-width:520px){.filters{grid-template-columns:repeat(2,1fr)}}
.ftile{background:var(--surface);border:2px solid transparent;border-radius:var(--radius);padding:14px 16px;cursor:pointer;transition:border-color .15s,background .15s;user-select:none}
.ftile:hover{border-color:var(--border)}
.ftile.active{border-color:var(--accent);background:var(--accent-bg)}
.ftile-n{font-size:28px;font-weight:700;line-height:1.1;letter-spacing:-.035em;font-variant-numeric:tabular-nums;margin-bottom:3px}
.ftile-l{font-size:11px;font-weight:500;letter-spacing:.02em;color:var(--fg2)}
.t-all .ftile-n{color:var(--fg)}
.t-upd .ftile-n{color:var(--upd-fg)}
.t-ok  .ftile-n{color:var(--ok-fg)}
.t-unk .ftile-n{color:var(--unk-fg)}
.toolbar{display:flex;align-items:center;justify-content:space-between;margin-bottom:12px;min-height:34px;gap:8px}
.sort-row{display:flex;align-items:center;gap:6px;font-size:12px;color:var(--fg2)}
.sort-btn{padding:4px 10px;border-radius:6px;border:1px solid var(--border);background:transparent;color:var(--fg2);font-family:inherit;font-size:11px;font-weight:500;cursor:pointer;transition:all .12s}
.sort-btn.active{background:var(--accent-bg);color:var(--accent);border-color:var(--accent)}
.btn-all{padding:8px 20px;border-radius:10px;border:none;background:var(--accent);color:#fff;font-family:inherit;font-size:13px;font-weight:600;cursor:pointer;transition:opacity .15s,transform .1s;box-shadow:0 2px 8px rgba(0,87,216,.25)}
.btn-all:hover{opacity:.9;transform:translateY(-1px)}
.btn-all:active{transform:none}
.btn-all:disabled{opacity:.5;cursor:default;transform:none;box-shadow:none}
.app-list{display:flex;flex-direction:column;gap:4px}
.app-card{background:var(--surface);border:1px solid var(--border2);border-radius:10px;padding:11px 14px;display:flex;align-items:center;gap:12px;transition:box-shadow .15s}
.app-card:hover{box-shadow:0 2px 8px rgba(0,0,0,.07)}
.app-card.needs-update{border-left:3px solid var(--upd-fg)}
.app-card.outdated{border-left:3px solid var(--crit-fg)}
.avatar{width:36px;height:36px;border-radius:9px;display:flex;align-items:center;justify-content:center;font-size:13px;font-weight:700;color:#fff;flex-shrink:0;letter-spacing:-.02em}
.app-info{flex:1;min-width:0}
.app-nm{font-weight:600;font-size:14px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.app-meta{display:flex;align-items:center;flex-wrap:wrap;gap:5px;margin-top:2px;font-size:11px;color:var(--fg2)}
.ver-cur{font-family:'JetBrains Mono',monospace;font-variant-numeric:tabular-nums;color:var(--fg2)}
.ver-arr{color:var(--fg3);padding:0 1px}
.ver-new{font-family:'JetBrains Mono',monospace;font-variant-numeric:tabular-nums;color:var(--upd-fg);font-weight:500}
.src-pill{font-size:10px;font-weight:500;padding:1px 7px;border-radius:99px;border:1px solid var(--border);background:var(--surface2);color:var(--fg3);white-space:nowrap;letter-spacing:.01em}
.badge{display:inline-flex;align-items:center;padding:3px 10px;border-radius:99px;font-size:11px;font-weight:600;letter-spacing:.02em;white-space:nowrap;cursor:default}
.b-upd{background:var(--upd-bg);color:var(--upd-fg)}
.b-crit{background:var(--crit-bg);color:var(--crit-fg)}
.b-ok{background:var(--ok-bg);color:var(--ok-fg)}
.b-unk{background:var(--unk-bg);color:var(--unk-fg)}
.b-sys{background:var(--unk-bg);color:var(--unk-fg)}
.badge-col{flex-shrink:0}
.btn-action{display:inline-flex;align-items:center;gap:5px;padding:6px 14px;border-radius:8px;font-family:inherit;font-size:12px;font-weight:600;cursor:pointer;transition:all .12s;white-space:nowrap;text-decoration:none}
.btn-do{background:var(--accent);color:#fff;border:none;box-shadow:0 1px 4px rgba(0,87,216,.2)}
.btn-do:hover{opacity:.88;transform:translateY(-1px)}
.btn-do:disabled{opacity:.5;cursor:default;transform:none;box-shadow:none}
.btn-do.done{background:var(--ok-bg);color:var(--ok-fg);box-shadow:none}
.btn-do.failed{background:var(--crit-bg);color:var(--crit-fg);box-shadow:none}
.btn-do.info{background:var(--upd-bg);color:var(--upd-fg);box-shadow:none;cursor:default;white-space:normal;max-width:260px;height:auto;padding:6px 10px;text-align:left;font-weight:400;font-size:11px;line-height:1.4}
.btn-store{background:transparent;color:var(--accent);border:1.5px solid var(--accent)}
.btn-store:hover{background:var(--accent-bg)}
.btn-visit{background:transparent;color:var(--fg2);border:1px solid var(--border)}
.btn-visit:hover{border-color:var(--fg2);color:var(--fg)}
.action-col{flex-shrink:0;display:flex;flex-direction:column;align-items:flex-end;gap:4px}
.changelog-toggle{font-size:10px;color:var(--accent);background:none;border:none;cursor:pointer;font-family:inherit;padding:2px 0;text-decoration:underline;text-underline-offset:2px}
.changelog-toggle:hover{opacity:.75}
.changelog-panel{display:none;margin-top:8px;padding:10px 12px;background:var(--surface2);border:1px solid var(--border2);border-radius:8px;font-size:11px;color:var(--fg2);line-height:1.6;max-width:340px;white-space:pre-wrap;word-break:break-word}
.changelog-panel.open{display:block}
.app-card-wrap{display:flex;flex-direction:column;gap:0}
.empty-state{text-align:center;padding:52px 24px}
.empty-icon{font-size:38px;margin-bottom:10px}
.empty-title{font-weight:600;font-size:16px;color:var(--fg);margin-bottom:5px}
.empty-desc{font-size:13px;color:var(--fg2)}
@media(max-width:580px){.badge-col{display:none}}
@media(max-width:420px){.app-card{padding:10px 12px;gap:9px}}
</style>
</head>
<body>

<div class="topbar">
  <div class="brand">
    <span class="brand-name">Mac App Checker</span>
    <span class="brand-sub" id="sub">Checking your apps…</span>
  </div>
  <button class="btn-quit" onclick="document.getElementById('quit-modal').classList.add('open')">Quit</button>
</div>

<div class="modal-backdrop" id="quit-modal">
  <div class="modal">
    <div class="modal-icon">🛑</div>
    <div class="modal-title">Stop Mac App Checker?</div>
    <div class="modal-desc">The dashboard will close and the background service will stop running.</div>
    <div class="modal-btns">
      <button class="modal-btn modal-cancel" onclick="document.getElementById('quit-modal').classList.remove('open')">Cancel</button>
      <button class="modal-btn modal-stop" onclick="location.href='/quit'">Stop</button>
    </div>
  </div>
</div>

<div class="main">
  <div class="macos-card" id="macos"></div>
  <div class="filters" id="filters"></div>
  <div class="toolbar" id="toolbar"></div>
  <div class="app-list" id="app-list"></div>
  <div class="empty-state" id="empty" style="display:none">
    <div class="empty-icon">🔍</div>
    <div class="empty-title">No apps match this filter</div>
    <div class="empty-desc">Try a different category above.</div>
  </div>
  <div style="margin-top:28px;text-align:center;font-size:11px;color:var(--fg3)" id="scan-footer"></div>
</div>

<script>
const D=__DATA__;
let currentFilter='all';
let currentSort='status';

const _SORT={behind:0,update:1,vendor:2,unknown:3,current:4,system:5};

function avatarColor(name){
  let h=0;
  for(let i=0;i<name.length;i++)h=name.charCodeAt(i)+((h<<5)-h);
  return'hsl('+(((h%360)+360)%360)+',50%,44%)';
}
function avatarLetters(name){
  const w=name.trim().split(/\s+/);
  return w.length>=2?(w[0][0]+w[1][0]).toUpperCase():name.slice(0,2).toUpperCase();
}

const SOURCE_LABELS={'App Store':'App Store','Homebrew Cask':'Homebrew','Sparkle':'Sparkle','Direct':'Direct','Apple':'Built-in'};
function friendlySrc(s){return SOURCE_LABELS[s]||s}

const STATUS={
  update: {cls:'b-upd', label:'Update available'},
  behind: {cls:'b-crit',label:'Far behind'},
  vendor: {cls:'b-unk', label:'Self-updating'},
  current:{cls:'b-ok',  label:'Up to date'},
  unknown:{cls:'b-unk', label:"Can\'t check"},
  system: {cls:'b-sys', label:'Built-in'},
};

function cleanVer(v){return v?v.split(',')[0]:''}
function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;')}

function formatScanTime(iso){
  if(!iso)return'';
  try{
    const d=new Date(iso);
    return d.toLocaleString('en-GB',{day:'numeric',month:'short',year:'numeric',hour:'2-digit',minute:'2-digit'});
  }catch(e){return iso}
}

function sortedApps(apps){
  const a=[...apps];
  if(currentSort==='name') return a.sort((x,y)=>x.name.localeCompare(y.name));
  return a.sort((x,y)=>{
    const xs=_SORT[x.status]??9, ys=_SORT[y.status]??9;
    return xs!==ys ? xs-ys : x.name.localeCompare(y.name);
  });
}

function toggleChangelog(uid){
  const p=document.getElementById('cl-'+uid);
  if(p){p.classList.toggle('open');const b=document.getElementById('clb-'+uid);if(b)b.textContent=p.classList.contains('open')?'Hide notes ▴':'What\'s new ▾';}
}

function render(){
  const apps=D.apps;
  // vendor counts as "needs action" for the tile and filter
  const needsUpd=apps.filter(a=>a.status==='update'||a.status==='behind'||a.status==='vendor');
  const upToDate=apps.filter(a=>a.status==='current');
  const cantChk =apps.filter(a=>a.status==='unknown'||a.status==='system');

  document.getElementById('sub').textContent=apps.length+' apps · '+formatScanTime(D.scan_time);

  const m=D.macos||{};
  document.getElementById('macos').innerHTML=m.has_update
    ?'<div class="macos-icon">🍎</div><div class="macos-info"><div class="macos-name">Your Mac · '+(m.version||'')+'</div><div class="macos-status upd">macOS update available: '+(m.updates||[]).slice(0,2).join(', ')+'</div></div>'
    :'<div class="macos-icon">🍎</div><div class="macos-info"><div class="macos-name">Your Mac · '+(m.version||'Unknown')+'</div><div class="macos-status ok">'+(m.version?'✓ Up to date':'Could not check')+'</div></div>';

  const tiles=[
    {k:'all',cls:'t-all',n:apps.length,     l:'All Apps'},
    {k:'upd',cls:'t-upd',n:needsUpd.length, l:needsUpd.length===1?'Needs Attention':'Need Attention'},
    {k:'ok', cls:'t-ok', n:upToDate.length, l:'Up to Date'},
    {k:'unk',cls:'t-unk',n:cantChk.length,  l:"Can\'t Check"},
  ];
  document.getElementById('filters').innerHTML=tiles.map(t=>
    '<div class="ftile '+t.cls+(t.k===currentFilter?' active':'')+'" onclick="setFilter(\''+t.k+'\')">'+
    '<div class="ftile-n">'+t.n+'</div><div class="ftile-l">'+t.l+'</div></div>'
  ).join('');

  const sortBtns=['status','name'].map(s=>
    '<button class="sort-btn'+(currentSort===s?' active':'')+'" onclick="setSort(\''+s+'\')">'+(s==='status'?'By status':'A–Z')+'</button>'
  ).join('');
  const autoUpdatable=needsUpd.filter(a=>a.status==='update'||a.status==='behind');
  document.getElementById('toolbar').innerHTML=
    '<div class="sort-row"><span>Sort:</span>'+sortBtns+'</div>'+
    (autoUpdatable.length>0?'<button class="btn-all" id="btn-all" onclick="updateAll()">Update All ('+autoUpdatable.length+')</button>':'');

  const fmap={all:null,upd:['update','behind','vendor'],ok:['current'],unk:['unknown','system']};
  const sf=fmap[currentFilter];
  const rows=sortedApps(sf ? apps.filter(a=>sf.includes(a.status)) : apps);
  const listEl=document.getElementById('app-list');
  const emptyEl=document.getElementById('empty');

  if(rows.length===0){listEl.innerHTML='';emptyEl.style.display='block';return}
  emptyEl.style.display='none';

  listEl.innerHTML=rows.map(a=>{
    const uid=a.name.replace(/[^a-z0-9]/gi,'-');
    const id='btn-'+uid;
    const si=STATUS[a.status]||STATUS.unknown;
    const lv=cleanVer(a.latest)||'';
    const isAutoUpd=a.status==='update'||a.status==='behind';
    const isVendor=a.status==='vendor';

    const verHtml=(isAutoUpd||isVendor)&&lv
      ?'<span class="ver-cur">'+esc(a.installed)+'</span><span class="ver-arr"> → </span><span class="ver-new">'+esc(lv)+'</span>'
      :'<span class="ver-cur">'+esc(a.installed)+'</span>';

    let actionHtml='';
    if(isAutoUpd){
      actionHtml='<button class="btn-action btn-do" id="'+id+'" onclick="doUpdate(this,\''+encodeURIComponent(a.name)+'\')">Update</button>';
    }else if(isVendor&&a.homepage){
      actionHtml='<a class="btn-action btn-visit" href="'+esc(a.homepage)+'" target="_blank" rel="noopener">Website ↗</a>';
    }else if(a.status==='system'){
      actionHtml='<a class="btn-action btn-store" href="macappstore://showUpdatesPage" target="_blank">App Store ↗</a>';
    }else if(a.status==='unknown'&&a.homepage){
      actionHtml='<a class="btn-action btn-visit" href="'+esc(a.homepage)+'" target="_blank" rel="noopener">Website ↗</a>';
    }

    // Changelog: show toggle only for auto-updatable apps with notes
    const hasNotes=a.changelog&&a.changelog.trim().length>0&&isAutoUpd;
    const notesToggle=hasNotes?'<button class="changelog-toggle" id="clb-'+uid+'" onclick="toggleChangelog(\''+uid+'\')">What\'s new ▾</button>':'';
    const notesPanel=hasNotes?'<div class="changelog-panel" id="cl-'+uid+'">'+esc(a.changelog.trim())+'</div>':'';

    const cardCls=a.status==='behind'?' outdated':(isAutoUpd?' needs-update':isVendor?' needs-update':'');

    return'<div class="app-card'+cardCls+'">'+
      '<div class="avatar" style="background:'+avatarColor(a.name)+'">'+esc(avatarLetters(a.name))+'</div>'+
      '<div class="app-info">'+
        '<div class="app-nm">'+esc(a.name)+'</div>'+
        '<div class="app-meta">'+verHtml+'<span class="src-pill">'+esc(friendlySrc(a.source))+'</span>'+(isVendor?'<span class="src-pill" title="This app updates itself">self-updating</span>':'')+'</div>'+
        (notesPanel||'')+
      '</div>'+
      '<div class="badge-col"><span class="badge '+si.cls+'">'+si.label+'</span></div>'+
      '<div class="action-col">'+actionHtml+(notesToggle?'<br>'+notesToggle:'')+'</div>'+
      '</div>';
  }).join('');

  const foot=document.getElementById('scan-footer');
  if(foot)foot.textContent=D.scan_time?'Scanned '+formatScanTime(D.scan_time)+' · v'+D.version:'v'+D.version;
}

function setFilter(v){currentFilter=v;render()}
function setSort(v){currentSort=v;render()}

async function doUpdate(btn,enc){
  const name=decodeURIComponent(enc);
  btn.disabled=true;btn.textContent='Updating…';
  try{
    const r=await fetch('/update',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name})});
    const d=await r.json();
    if(d.success){
      btn.textContent='✓ Done';btn.title=d.msg||'';
      btn.classList.remove('btn-do');btn.classList.add('done');
    }else if(d.info){
      btn.textContent='ℹ️ Info';btn.title='';
      btn.classList.remove('btn-do');btn.classList.add('info');btn.disabled=false;
      if(!btn.nextElementSibling||btn.nextElementSibling.className!=='info-msg'){
        const msgEl=document.createElement('div');
        msgEl.className='info-msg';
        msgEl.style.cssText='margin-top:4px;font-size:11px;color:var(--upd-fg);max-width:240px;line-height:1.4;white-space:normal';
        msgEl.textContent=d.msg||'';
        btn.parentNode.insertBefore(msgEl,btn.nextSibling);
      }
    }else{
      btn.textContent='✕ Failed';btn.title=d.msg||'';
      btn.classList.remove('btn-do');btn.classList.add('failed');btn.disabled=false;
    }
  }catch(e){
    btn.textContent='✕ Error';
    btn.classList.remove('btn-do');btn.classList.add('failed');btn.disabled=false;
  }
}

async function updateAll(){
  const btn=document.getElementById('btn-all');
  if(!btn)return;
  btn.disabled=true;btn.textContent='Updating…';
  const apps=D.apps.filter(a=>a.status==='update'||a.status==='behind');
  for(const a of apps){
    const b=document.getElementById('btn-'+a.name.replace(/[^a-z0-9]/gi,'-'));
    if(b&&!b.classList.contains('done'))await doUpdate(b,encodeURIComponent(a.name));
  }
  btn.textContent='All done ✓';
}

render();
</script>
</body></html>"""

def build_html(results, macos_info, scan_time=None):
    data = json.dumps({'apps': results, 'macos': macos_info, 'scan_time': scan_time or '', 'version': __version__})
    return HTML_TEMPLATE.replace('__DATA__', data)

# ── App scan: per-app worker ──────────────────────────────────────────────────
def check_one_app(app):
    """Check one app's version. Returns result dict or None if hidden."""
    bundle_id = app['bundle_id']
    name      = app['name']

    if name in HIDE_APP_NAMES or bundle_id in HIDE_BUNDLE_IDS:
        return None

    latest = cask_token = apple_id = None
    homepage  = ''
    changelog = ''
    mas_app = False
    vendor_managed = is_vendor_managed(bundle_id, name)

    if bundle_id in APPLE_SYSTEM_BUNDLES:
        source = 'Apple'

    elif bundle_id.startswith(MICROSOFT_BUNDLE_PREFIX):
        # Use build_version for Microsoft apps (CFBundleVersion matches cask)
        if app.get('build_version'):
            app = {**app, 'version': app['build_version']}
        token = normalize_to_token(name)
        cask  = fetch_cask(token)
        if cask:
            latest     = cask.get('version')
            homepage   = cask.get('homepage', '')
            source     = 'Homebrew Cask'
            cask_token = token
        else:
            source = 'Direct'

    elif is_app_store_app(app['path']):
        for country in ('us', 'gb'):
            d = fetch_url(f'https://itunes.apple.com/lookup?bundleId={bundle_id}&country={country}')
            if d and d.get('resultCount', 0) > 0:
                # Prefer mac-software entries; skip iOS-only entries (Catalyst apps share
                # a bundle ID but the iOS version number is completely different)
                mac_result = next((r for r in d['results'] if r.get('kind') == 'mac-software'), None)
                r = mac_result or d['results'][0]
                if r.get('kind') == 'software' and not mac_result:
                    # iOS-only result — version is untrustworthy for Mac, skip iTunes
                    break
                latest    = r.get('version')
                apple_id  = str(r.get('trackId', ''))
                changelog = (r.get('releaseNotes') or '')[:800]
                break
        # If iTunes gave no usable Mac version, try Sparkle (Catalyst apps often have one)
        if not latest:
            feed_url = app.get('sparkle_url', '')
            if feed_url:
                sparkle_ver, sparkle_notes = fetch_sparkle(feed_url)
                if sparkle_ver:
                    latest    = sparkle_ver
                    changelog = (sparkle_notes or '')[:800]
                    source    = 'Sparkle'
                    LOG.info(f'{name}: iTunes returned iOS version; using Sparkle {sparkle_ver!r}')
        if source != 'Sparkle':
            source = 'App Store'
        homepage = f'https://apps.apple.com/app/id{apple_id}' if apple_id else f'https://itunes.apple.com/app?bundleId={bundle_id}'
        # Always flag as App Store for update purposes, regardless of version-detection source
        mas_app = True

    else:
        token = normalize_to_token(name)
        cask  = fetch_cask(token)
        if cask:
            latest     = cask.get('version')
            homepage   = cask.get('homepage', '')
            source     = 'Homebrew Cask'
            cask_token = token
        else:
            # Try Sparkle feed as fallback for direct-download indie apps
            feed_url = app.get('sparkle_url', '')
            if feed_url:
                sparkle_ver, sparkle_notes = fetch_sparkle(feed_url)
                if sparkle_ver:
                    latest    = sparkle_ver
                    changelog = (sparkle_notes or '')[:800]
                    source    = 'Sparkle'
                else:
                    source = 'Direct'
            else:
                source = 'Direct'
            homepage = HOMEPAGE_OVERRIDES.get(name.lower(), '')

    LOG.info(f'{name}: installed={app["version"]!r}  latest={latest!r}  source={source}  vendor_managed={vendor_managed}')

    return {
        'name':           name,
        'installed':      app['version'],
        'bundle_id':      bundle_id,
        'path':           app['path'],
        'latest':         latest or '',
        'source':         source,
        'homepage':       homepage,
        'cask_token':     cask_token or '',
        'apple_id':       apple_id or '',
        'changelog':      changelog,
        'vendor_managed': vendor_managed,
        'mas_app':        mas_app,
    }

def scan_apps(progress_cb=None):
    """Scan all apps concurrently. Returns (results, macos_info)."""
    LOG.info('=== Scan started ===')

    # macOS check runs in parallel with app scan
    macos_result = {}
    macos_thread = threading.Thread(
        target=lambda: macos_result.update(check_macos()), daemon=True
    )
    macos_thread.start()

    apps  = find_apps()
    total = len(apps)
    LOG.info(f'Found {total} apps in /Applications')

    results    = []
    done_count = 0
    lock       = threading.Lock()

    def _check(app):
        nonlocal done_count
        try:
            result = check_one_app(app)
        except Exception as e:
            LOG.error(f'Error checking {app["name"]}: {e}')
            result = None
        with lock:
            done_count += 1
            n = done_count
            if result:
                results.append(result)
        if progress_cb:
            progress_cb(n, total, app['name'])

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(_check, apps))

    macos_thread.join(timeout=40)
    for r in results:
        r['status'] = get_status(r)
    _SORT_ORDER = {'behind': 0, 'update': 1, 'vendor': 2, 'unknown': 3, 'current': 4, 'system': 5}
    results.sort(key=lambda x: (_SORT_ORDER.get(x['status'], 9), x['name'].lower()))

    needs = sum(1 for r in results if r['status'] in ('update', 'behind'))
    LOG.info(f'Scan complete: {len(results)} apps, {needs} need updates')
    scan_time = time.strftime('%Y-%m-%dT%H:%M:%S')
    return results, macos_result, scan_time

# ── HTTP dashboard server ─────────────────────────────────────────────────────
def serve_dashboard():
    """Start HTTP server immediately, run scan in background, show progress then results."""
    from http.server import BaseHTTPRequestHandler
    from socketserver import ThreadingTCPServer

    try:
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
    except (AttributeError, OSError):
        pass

    state = {
        'scanning':  True,
        'done':      0,
        'total':     0,
        'current':   'Starting…',
        'results':   None,
        'macos':     None,
        'scan_time': None,
        'server':    None,
    }

    def on_progress(done, total, app_name):
        state['done']    = done
        state['total']   = total
        state['current'] = app_name

    def run_scan():
        results, macos, scan_time = scan_apps(progress_cb=on_progress)
        state['results']   = results
        state['macos']     = macos
        state['scan_time'] = scan_time
        state['scanning']  = False

    scan_thread = threading.Thread(target=run_scan, daemon=True)

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass

        def _send(self, body, content_type='text/html; charset=utf-8', code=200):
            try:
                self.send_response(code)
                self.send_header('Content-Type', content_type)
                self.end_headers()
                self.wfile.write(body if isinstance(body, bytes) else body.encode())
            except (BrokenPipeError, OSError):
                pass

        def do_GET(self):
            try:
                if self.path == '/quit':
                    self._send(b'<html><body style="font-family:sans-serif;padding:40px"><h2>Mac App Checker stopped.</h2><p>You can close this tab.</p></body></html>')
                    if state['server']:
                        threading.Thread(target=state['server'].shutdown, daemon=True).start()
                    return
                if self.path == '/api/status':
                    self._send(json.dumps({
                        'scanning': state['scanning'],
                        'done':     state['done'],
                        'total':    state['total'],
                        'app':      state['current'],
                    }), 'application/json')
                    return
                if state['scanning']:
                    self._send(SCANNING_TEMPLATE)
                else:
                    self._send(build_html(state['results'], state['macos'], state['scan_time']))
            except (BrokenPipeError, OSError):
                pass

        def do_OPTIONS(self):
            try:
                self.send_response(204)
                self.send_header('Access-Control-Allow-Origin', '*')
                self.send_header('Access-Control-Allow-Methods', 'POST, GET, OPTIONS')
                self.send_header('Access-Control-Allow-Headers', 'Content-Type')
                self.end_headers()
            except (BrokenPipeError, OSError):
                pass

        def do_POST(self):
            if self.path != '/update':
                return
            try:
                length = int(self.headers.get('Content-Length', 0))
                body   = json.loads(self.rfile.read(length) or b'{}')
                name   = body.get('name', '')
                app    = next((a for a in (state['results'] or []) if a['name'] == name), None)
                if app:
                    result  = do_update(app)
                    success = result[0]
                    msg     = result[1]
                    info    = result[2] if len(result) > 2 else False
                    resp = json.dumps({'success': success, 'msg': msg, 'info': info})
                else:
                    resp = json.dumps({'success': False, 'msg': 'App not found'})
            except Exception as e:
                LOG.error(f'POST /update error: {e}')
                resp = json.dumps({'success': False, 'msg': str(e)})
            try:
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(resp.encode())
            except (BrokenPipeError, OSError):
                pass

    ThreadingTCPServer.allow_reuse_address = True
    server = None
    port   = 8765
    for p in [8765, 8766, 8767, 8768, 8769]:
        try:
            server = ThreadingTCPServer(('localhost', p), H)
            port   = p
            break
        except OSError:
            continue

    if server is None:
        safe_print('Could not bind to any port 8765-8769.')
        return

    state['server'] = server
    url = f'http://localhost:{port}'
    safe_print(BLD(f'\nMac App Checker  v{__version__}'))
    safe_print(GRY(f'Dashboard at {url}'))
    LOG.info(f'Server started on {url}')

    # Open browser first, then start the scan so the user sees the progress page
    open_dashboard_browser(url)
    scan_thread.start()

    try:
        server.serve_forever()
    except (KeyboardInterrupt, BrokenPipeError, OSError):
        pass
    server.shutdown()
    LOG.info('Server stopped')

# ── Main ──────────────────────────────────────────────────────────────────────
STATUS_LABEL = {
    'update':  YEL('Update available'),
    'behind':  RED('Far behind'),
    'vendor':  GRY('Vendor-managed'),
    'current': GRN('Current'),
    'unknown': GRY('Unknown'),
    'system':  GRY('System app'),
}

def main():
    # Ignore SIGHUP early so the process survives when the .app terminal closes
    try:
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
    except (AttributeError, OSError):
        pass

    json_mode = '--json' in sys.argv
    gui_mode  = '--gui'  in sys.argv

    if gui_mode:
        kill_old_instances()

    if not json_mode:
        safe_print(BLD(f'\nMac App Checker  v{__version__}'))
        safe_print(GRY('Checking macOS and scanning /Applications…\n'))

    if gui_mode:
        serve_dashboard()
        return

    # Terminal and JSON modes still scan synchronously (no progress callback needed)
    results, macos_info, scan_time = scan_apps()

    if json_mode:
        print(json.dumps({'macos': macos_info, 'apps': results, 'scan_time': scan_time}, indent=2))
        return

    # ── Interactive Terminal mode ─────────────────────────────────────────────
    m = macos_info
    if m.get('has_update'):
        upd_names = ', '.join(m['updates'][:2]) + (' …' if len(m['updates']) > 2 else '')
        print(f"macOS  {YEL(m.get('version','?'))}  {YEL('Update available:')} {upd_names}")
    elif m.get('version'):
        print(f"macOS  {GRN(m['version'])}  {GRN('Up to date')}")
    else:
        print(f"macOS  {GRY('Could not check')}")

    needs = [r for r in results if r['status'] in ('update', 'behind')]
    ok    = [r for r in results if r['status'] == 'current']
    unk   = [r for r in results if r['status'] == 'unknown']

    print(f"\nScanned {len(results)} apps — "
          f"{YEL(str(len(needs)))} need updates · "
          f"{GRN(str(len(ok)))} up to date · "
          f"{GRY(str(len(unk)))} unknown\n")

    if not needs:
        print(GRN('All apps are up to date!'))
        return

    W = 70
    print(BLD('UPDATES AVAILABLE'))
    print(GRY('─' * W))
    print(f"  {'#':<4}{'App':<28}{'Installed':<16}{'Latest'}")
    print(GRY('─' * W))
    for i, app in enumerate(needs, 1):
        cv = clean_ver(app.get('latest', '')) or '—'
        print(f"  {i:<4}{app['name']:<28}{app['installed']:<16}{cv:<16}  {STATUS_LABEL[app['status']]}")
    print(GRY('─' * W))

    if ok:
        names = ', '.join(r['name'] for r in ok[:7]) + (f' +{len(ok)-7} more' if len(ok) > 7 else '')
        print(f"\n{GRN('UP TO DATE:')} {GRY(names)}")
    if unk:
        names = ', '.join(r['name'] for r in unk[:5]) + (f' +{len(unk)-5} more' if len(unk) > 5 else '')
        print(f"{GRY('UNKNOWN:')} {GRY(names)}")

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
                print(GRY(f'  Please enter a number between 1 and {len(needs)}.')); return
        except ValueError:
            print(GRY('  Invalid input.')); return

    for app in to_update:
        cv = clean_ver(app.get('latest', ''))
        print(f"\n{BLD('Updating')} {app['name']}  {GRY(app['installed'])} → {GRN(cv)}")
        success, msg = do_update(app)
        if success:
            print(GRN(f'  ✓ {msg}'))
        else:
            print(RED(f'  ✗ {msg}'))

    print(f'\n{GRN("Done.")}\n')

if __name__ == '__main__':
    main()
