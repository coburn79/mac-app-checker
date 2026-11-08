#!/usr/bin/env python3
"""Mac App Checker - scans /Applications and checks for updates"""

__version__ = "1.8.0"

import os, plistlib, json, subprocess, time, re, sys, tempfile, shutil, threading, signal

# ── Colour helpers (terminal mode only) ──────────────────────────────────────
USE_COLOR = sys.stdout.isatty()
def _c(code, t): return f"\033[{code}m{t}\033[0m" if USE_COLOR else t
RED = lambda t: _c("91", t)
YEL = lambda t: _c("93", t)
GRN = lambda t: _c("92", t)
GRY = lambda t: _c("90", t)
BLD = lambda t: _c("1",  t)

def kill_old_instances():
    """Kill any other running copies of this script so only one server runs."""
    my_pid = os.getpid()
    try:
        r = subprocess.run(['pgrep', '-f', 'app_checker.py'], capture_output=True, text=True)
        for pid_str in r.stdout.strip().split():
            pid = int(pid_str)
            if pid != my_pid:
                try:
                    os.kill(pid, signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
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

# Microsoft Office apps use AutoUpdate, not Homebrew Cask downloads
MICROSOFT_CASKS = {
    'microsoft-word', 'microsoft-excel', 'microsoft-powerpoint',
    'microsoft-onenote', 'microsoft-outlook', 'microsoft-teams',
    'microsoft-edge',
}
MICROSOFT_AUTOUPDATE = '/Library/Application Support/Microsoft/MAU2.0/Microsoft AutoUpdate.app'

# Apps whose bundle ID starts with com.microsoft.* → always use Homebrew cask
# (some have App Store receipts from mixed installs, but cask has the real version)
MICROSOFT_BUNDLE_PREFIX = 'com.microsoft.'

# Apple system/bundled apps that update with macOS or via Mac App Store
# iTunes lookup API returns nothing for these, so we show them as "System"
APPLE_SYSTEM_BUNDLES = {
    'com.apple.Safari', 'com.apple.iWork.Keynote', 'com.apple.iWork.Numbers',
    'com.apple.iWork.Pages', 'com.apple.mail', 'com.apple.Photos',
}

# Apps to hide — noise the user doesn't need to see
HIDE_BUNDLE_IDS = {''}          # catches apps with no bundle ID (e.g. our own .app)
HIDE_APP_NAMES  = {'Mac App Checker', 'Microsoft AutoUpdate'}

# Manual homepage URLs for direct-download apps that have no Homebrew cask
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

def fetch_cask(token):
    d = fetch_url(f'https://formulae.brew.sh/api/cask/{token}.json')
    return d if d and 'version' in d else None

def fetch_mas(bundle_id):
    for country in ('us', 'gb'):
        d = fetch_url(f'https://itunes.apple.com/lookup?bundleId={bundle_id}&country={country}')
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
            'name':          p.get('CFBundleName') or p.get('CFBundleDisplayName') or os.path.basename(path).replace('.app', ''),
            'version':       p.get('CFBundleShortVersionString', 'Unknown'),
            'build_version': p.get('CFBundleVersion', ''),
            'bundle_id':     p.get('CFBundleIdentifier', ''),
            'path':          path,
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
    if app.get('source') == 'Apple':
        return 'system'
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

def _ditto_install(src_app, app_name):
    """Copy src_app into /Applications using ditto (preserves macOS metadata)."""
    dest = f'/Applications/{os.path.basename(src_app)}'
    _quit_app(app_name)
    if os.path.exists(dest):
        r = subprocess.run(['rm', '-rf', dest], capture_output=True)
        if r.returncode != 0:
            return False, 'Permission denied removing old version'
    r = subprocess.run(['ditto', src_app, dest], capture_output=True, text=True)
    if r.returncode != 0:
        return False, r.stderr.strip() or 'Install failed (permission denied?)'
    subprocess.run(['xattr', '-cr', dest], capture_output=True)
    return True, f'Installed {os.path.basename(src_app)}'

def _install_from_url(url, app_name):
    """Download url, detect DMG or ZIP, install the .app. Returns (success, message)."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        dl = os.path.join(tmp_dir, 'download')
        safe_print(f"  Downloading {app_name}...")
        r = subprocess.run(['curl', '-L', '-s', '-o', dl, url], timeout=600)
        if r.returncode != 0 or not os.path.exists(dl):
            return False, 'Download failed'

        # Detect file type from magic bytes (not URL extension)
        ft = subprocess.run(['file', dl], capture_output=True, text=True).stdout.lower()
        is_dmg = any(k in ft for k in ('apple disk image', 'udif', 'binhex'))
        is_zip = any(k in ft for k in ('zip archive', 'jar'))

        if not is_dmg and not is_zip:
            # Fallback: check URL for hints
            u = url.lower().split('?')[0]
            is_dmg = u.endswith('.dmg')
            is_zip = u.endswith('.zip')

        if is_dmg:
            safe_print(f"  Mounting disk image...")
            mr = subprocess.run(
                ['hdiutil', 'attach', '-nobrowse', '-noverify', dl],
                capture_output=True, text=True
            )
            if mr.returncode != 0:
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
                safe_print(f"  Installing {os.path.basename(app_src)}...")
                return _ditto_install(app_src, app_name)
            except Exception as e:
                return False, str(e)
            finally:
                subprocess.run(['hdiutil', 'detach', mount_point, '-quiet'],
                               capture_output=True)

        elif is_zip:
            extract_dir = os.path.join(tmp_dir, 'x')
            os.makedirs(extract_dir)
            subprocess.run(['unzip', '-q', dl, '-d', extract_dir], capture_output=True)
            app_src = None
            for root, dirs, _ in os.walk(extract_dir):
                for d in dirs:
                    if d.endswith('.app'):
                        app_src = os.path.join(root, d); break
                if app_src:
                    break
            if not app_src:
                return False, 'No .app found in ZIP'
            safe_print(f"  Installing {os.path.basename(app_src)}...")
            return _ditto_install(app_src, app_name)

        else:
            return False, 'Unknown file format (not DMG or ZIP)'

def do_update(app):
    """Trigger an update for the app. Returns (success, message)."""
    token = app.get('cask_token', '')

    # Microsoft Office → AutoUpdate tool
    if token in MICROSOFT_CASKS:
        if os.path.exists(MICROSOFT_AUTOUPDATE):
            safe_print(f"  Opening Microsoft AutoUpdate for {app['name']}...")
            subprocess.run(['open', MICROSOFT_AUTOUPDATE])
            return True, 'Microsoft AutoUpdate opened'
        safe_print(f"  Opening {app['name']}...")
        subprocess.run(['open', '-a', app['name']])
        return True, 'App opened'

    if app['source'] in ('App Store', 'Apple'):
        safe_print(f"  Opening App Store updates page...")
        subprocess.run(['open', 'macappstore://showUpdatesPage'])
        return True, 'App Store opened'

    if not token:
        # No cask found — open the app so it can self-update
        safe_print(f"  Opening {app['name']} to self-update...")
        subprocess.run(['open', '-a', app['name']])
        return True, 'App opened — check for updates inside the app'

    safe_print(f"  Fetching download info for {app['name']}...", flush=True)
    cask = fetch_cask(token)
    if not cask:
        return False, 'Could not fetch download info'

    url = cask.get('url', '')
    if not url:
        homepage = cask.get('homepage', '')
        if homepage:
            subprocess.run(['open', homepage])
            return True, 'Download page opened'
        return False, 'No download URL found'

    return _install_from_url(url, app['name'])

# ── HTML dashboard ────────────────────────────────────────────────────────────
HTML_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Mac App Checker</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=DM+Serif+Text&family=JetBrains+Mono:wght@400;500&family=DM+Sans:wght@400;500;600&display=swap">
<style>
:root{
  --bg:#f7f5f2;--bg2:#edeae6;--bg3:#fff;
  --fg:#1c1917;--fg2:#6b7280;--border:#e2ddd8;
  --accent:#2563eb;
  --upd:#b45309;--upd-bg:#fef3c7;
  --ok:#15803d;--ok-bg:#f0fdf4;
  --crit:#b91c1c;--crit-bg:#fff1f1;
  --unk:#64748b;--unk-bg:#f1f5f9;
}
@media(prefers-color-scheme:dark){
  :root{
    --bg:#111318;--bg2:#1c2030;--bg3:#1c2030;
    --fg:#e9ecef;--fg2:#7a8899;--border:#2a3245;
    --accent:#60a5fa;
    --upd:#fbbf24;--upd-bg:#2c1900;
    --ok:#34d399;--ok-bg:#052e16;
    --crit:#f87171;--crit-bg:#2d0808;
    --unk:#64748b;--unk-bg:#1c2030;
  }
}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
body{font-family:'DM Sans',system-ui,sans-serif;background:var(--bg);color:var(--fg);font-size:14px;line-height:1.5;padding:32px 24px;max-width:940px;margin:0 auto}
h1{font-family:'DM Serif Text',serif;font-size:30px;font-weight:400;margin-bottom:4px;letter-spacing:-.01em}
.sub{color:var(--fg2);font-size:13px;margin-bottom:20px}
.macos-bar{display:flex;align-items:center;gap:10px;padding:10px 14px;background:var(--bg3);border:1px solid var(--border);border-radius:10px;margin-bottom:20px;font-size:13px}
.macos-label{color:var(--fg2);font-size:11px;text-transform:uppercase;letter-spacing:.06em}
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:24px}
@media(max-width:520px){.stats{grid-template-columns:repeat(2,1fr)}}
.stat{background:var(--bg3);border:1px solid var(--border);border-radius:12px;padding:14px 16px;cursor:pointer;transition:border-color .15s}
.stat:hover,.stat.active{border-color:var(--accent);box-shadow:0 0 0 1px var(--accent)}
.stat-num{font-family:'JetBrains Mono',monospace;font-size:26px;font-weight:500;line-height:1;margin-bottom:5px;font-variant-numeric:tabular-nums}
.stat-label{font-size:11px;color:var(--fg2);text-transform:uppercase;letter-spacing:.06em}
.stat-all .stat-num{color:var(--fg)}
.stat-upd .stat-num{color:var(--upd)}
.stat-ok  .stat-num{color:var(--ok)}
.stat-unk .stat-num{color:var(--unk)}
.toolbar{display:flex;justify-content:flex-end;margin-bottom:12px}
.btn-all{padding:8px 18px;border-radius:8px;border:none;background:var(--accent);color:#fff;font-family:'DM Sans',sans-serif;font-size:13px;font-weight:600;cursor:pointer;transition:opacity .15s}
.btn-all:hover{opacity:.85}
.btn-all:disabled{opacity:.5;cursor:default}
.btn-quit{padding:8px 14px;border-radius:8px;border:1px solid var(--border);background:transparent;color:var(--fg2);font-family:'DM Sans',sans-serif;font-size:13px;cursor:pointer;transition:all .15s}
.btn-quit:hover{border-color:var(--crit);color:var(--crit)}
.shell{background:var(--bg3);border:1px solid var(--border);border-radius:12px;overflow:hidden}
.wrap{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:13px}
thead{background:var(--bg2)}
th{text-align:left;padding:10px 16px;font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.07em;color:var(--fg2);border-bottom:1px solid var(--border);white-space:nowrap}
td{padding:11px 16px;border-bottom:1px solid var(--border);vertical-align:middle}
tr:last-child td{border-bottom:none}
tr:hover td{background:var(--bg2)}
.app-name{font-weight:500}
.ver{font-family:'JetBrains Mono',monospace;font-size:12px;font-variant-numeric:tabular-nums;color:var(--fg2)}
.ver.i{color:var(--fg)}
.arrow{color:var(--fg2);padding:0 4px;font-size:11px}
.vc{display:flex;align-items:center;flex-wrap:wrap;gap:2px}
.badge{display:inline-block;padding:3px 10px;border-radius:99px;font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.05em;white-space:nowrap}
.b-upd{background:var(--upd-bg);color:var(--upd)}
.b-crit{background:var(--crit-bg);color:var(--crit)}
.b-ok{background:var(--ok-bg);color:var(--ok)}
.b-unk{background:var(--unk-bg);color:var(--unk)}
.b-sys{background:var(--unk-bg);color:var(--unk)}
.btn-upd{display:inline-block;padding:4px 12px;border-radius:6px;border:1px solid var(--border);background:transparent;color:var(--fg2);font-family:'DM Sans',sans-serif;font-size:12px;cursor:pointer;transition:all .15s;white-space:nowrap;text-decoration:none}
.btn-upd:hover{border-color:var(--accent);color:var(--accent)}
.btn-upd:disabled{opacity:.5;cursor:default}
.btn-upd.ok{border-color:var(--ok);color:var(--ok)}
.btn-upd.err{border-color:var(--crit);color:var(--crit)}
.empty{text-align:center;padding:48px;color:var(--fg2);font-style:italic}
@media(max-width:600px){.col-src{display:none}}
</style>
</head>
<body>
<h1>Mac App Checker</h1>
<p class="sub" id="sub"></p>
<div class="macos-bar" id="macos"></div>
<div class="stats" id="stats"></div>
<div class="toolbar" id="toolbar" style="justify-content:space-between">
  <button class="btn-quit" onclick="if(confirm('Stop Mac App Checker?'))location.href='/quit'">Quit</button>
  <button class="btn-all" id="btn-all" onclick="updateAll()" hidden>Update All</button>
</div>
<div class="shell">
  <div class="wrap">
    <table>
      <thead><tr>
        <th>App</th><th>Versions</th>
        <th class="col-src">Source</th>
        <th>Status</th><th></th>
      </tr></thead>
      <tbody id="tbody"></tbody>
    </table>
    <div class="empty" id="empty" hidden>No apps match this filter.</div>
  </div>
</div>
<script>
const D=__DATA__;
let f='all';
const cv=v=>v?v.split(',')[0]:'';
const BM={update:'b-upd',behind:'b-crit',current:'b-ok',unknown:'b-unk',system:'b-sys'};
const BL={update:'Update',behind:'Far behind',current:'Current',unknown:'Unknown',system:'System'};

function render(){
  const all=D.apps, upd=all.filter(a=>a.status==='update'||a.status==='behind'),
        ok=all.filter(a=>a.status==='current'), unk=all.filter(a=>a.status==='unknown'||a.status==='system');
  document.getElementById('sub').textContent=
    `Scanned ${all.length} apps · ${new Date().toLocaleDateString('en-GB',{day:'numeric',month:'long',year:'numeric'})}`;
  const m=D.macos||{};
  document.getElementById('macos').innerHTML=m.has_update
    ?`<span class="macos-label">macOS</span><b>${m.version}</b><span style="color:var(--upd)">Update available: ${(m.updates||[]).slice(0,2).join(', ')}</span>`
    :`<span class="macos-label">macOS</span><b>${m.version||'—'}</b><span style="color:var(--ok)">${m.version?'✓ Up to date':'Could not check'}</span>`;
  const defs=[
    {k:'all',cls:'stat-all',n:all.length,l:'All apps'},
    {k:'upd',cls:'stat-upd',n:upd.length,l:'Need updates'},
    {k:'ok', cls:'stat-ok', n:ok.length, l:'Up to date'},
    {k:'unk',cls:'stat-unk',n:unk.length,l:'Unknown'},
  ];
  document.getElementById('stats').innerHTML=defs.map(d=>
    `<div class="stat ${d.cls}${d.k===f?' active':''}" onclick="setF('${d.k}')">
      <div class="stat-num">${d.n}</div><div class="stat-label">${d.l}</div></div>`).join('');
  document.getElementById('btn-all').hidden=upd.length===0;
  const fm={all:null,upd:['update','behind'],ok:['current'],unk:['unknown','system']};
  const rows=fm[f]?all.filter(a=>fm[f].includes(a.status)):all;
  document.getElementById('empty').hidden=rows.length>0;
  document.getElementById('tbody').innerHTML=rows.map(a=>{
    const v=cv(a.latest)||'';
    const vc=a.status==='current'||!v
      ?`<span class="ver i">${a.installed}</span>`
      :`<span class="ver i">${a.installed}</span><span class="arrow">→</span><span class="ver">${v}</span>`;
    const id='b-'+a.name.replace(/\s+/g,'-');
    const btn=(a.status==='update'||a.status==='behind')
      ?`<button class="btn-upd" id="${id}" onclick="doUpdate(this,'${encodeURIComponent(a.name)}')">Update</button>`
      :(a.status==='system')
      ?`<button class="btn-upd" id="${id}" onclick="doUpdate(this,'${encodeURIComponent(a.name)}')">App Store</button>`
      :(a.status==='unknown'&&a.homepage)
      ?`<a class="btn-upd" href="${a.homepage}" target="_blank" rel="noopener">Visit ↗</a>`
      :'';
    return `<tr><td><span class="app-name">${a.name}</span></td>
      <td><div class="vc">${vc}</div></td>
      <td class="col-src" style="color:var(--fg2);font-size:12px">${a.source}</td>
      <td><span class="badge ${BM[a.status]}">${BL[a.status]}</span></td>
      <td>${btn}</td></tr>`;
  }).join('');
}

function setF(v){f=v;render()}

async function doUpdate(btn,enc){
  const name=decodeURIComponent(enc);
  btn.disabled=true; btn.textContent='Updating…';
  try{
    const r=await fetch('/update',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name})});
    const d=await r.json();
    if(d.success){btn.textContent='✓ Done';btn.title=d.msg||'';btn.classList.add('ok')}
    else{btn.textContent='✗ Failed';btn.title=d.msg||'';btn.classList.add('err');btn.disabled=false}
  }catch(e){btn.textContent='✗ Error';btn.classList.add('err');btn.disabled=false}
}

async function updateAll(){
  const btn=document.getElementById('btn-all');
  btn.disabled=true; btn.textContent='Updating…';
  const apps=D.apps.filter(a=>a.status==='update'||a.status==='behind');
  for(const a of apps){
    const b=document.getElementById('b-'+a.name.replace(/\s+/g,'-'));
    if(b&&!b.classList.contains('ok'))await doUpdate(b,encodeURIComponent(a.name));
  }
  btn.textContent='Done';
}
render();
</script>
</body></html>"""

def build_html(results, macos_info):
    data = json.dumps({'apps': results, 'macos': macos_info})
    return HTML_TEMPLATE.replace('__DATA__', data)

# ── HTTP dashboard server ─────────────────────────────────────────────────────
def serve_dashboard(results, macos_info):
    from http.server import BaseHTTPRequestHandler
    from socketserver import ThreadingTCPServer

    # Survive terminal close: ignore SIGHUP so the server keeps running
    # even after the .app launcher window closes
    try:
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
    except (AttributeError, OSError):
        pass

    app_data = {'results': results, 'macos': macos_info, 'server': None}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass

        def do_GET(self):
            if self.path == '/quit':
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.end_headers()
                self.wfile.write(b'<html><body style="font-family:sans-serif;padding:40px"><h2>Mac App Checker stopped.</h2><p>You can close this tab.</p></body></html>')
                if app_data['server']:
                    threading.Thread(target=app_data['server'].shutdown, daemon=True).start()
                return
            html = build_html(app_data['results'], app_data['macos'])
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(html.encode())

        def do_OPTIONS(self):
            self.send_response(204)
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Access-Control-Allow-Methods', 'POST, GET, OPTIONS')
            self.send_header('Access-Control-Allow-Headers', 'Content-Type')
            self.end_headers()

        def do_POST(self):
            if self.path == '/update':
                try:
                    length = int(self.headers.get('Content-Length', 0))
                    body   = json.loads(self.rfile.read(length) or b'{}')
                    name   = body.get('name', '')
                    app    = next((a for a in app_data['results'] if a['name'] == name), None)
                    if app:
                        success, msg = do_update(app)
                        resp = json.dumps({'success': success, 'msg': msg})
                    else:
                        resp = json.dumps({'success': False, 'msg': 'App not found'})
                except Exception as e:
                    resp = json.dumps({'success': False, 'msg': str(e)})
                try:
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Access-Control-Allow-Origin', '*')
                    self.end_headers()
                    self.wfile.write(resp.encode())
                except (BrokenPipeError, OSError):
                    pass

    # Find an open port
    ThreadingTCPServer.allow_reuse_address = True
    server = None
    port = 8765
    for p in [8765, 8766, 8767, 8768, 8769]:
        try:
            server = ThreadingTCPServer(('localhost', p), H)
            port = p
            break
        except OSError:
            continue

    if server is None:
        safe_print("Could not bind to any port 8765-8769.")
        return

    app_data['server'] = server
    url = f'http://localhost:{port}'
    safe_print(BLD(f"\nMac App Checker  v{__version__}"))
    safe_print(GRY(f"Dashboard at {url}  (close the browser tab or visit {url}/quit to stop)"))
    open_dashboard_browser(url)
    try:
        server.serve_forever()
    except (KeyboardInterrupt, BrokenPipeError, OSError):
        pass
    server.shutdown()

# ── Main ──────────────────────────────────────────────────────────────────────
STATUS_LABEL = {
    'update':  YEL('Update available'),
    'behind':  RED('Far behind'),
    'current': GRN('Current'),
    'unknown': GRY('Unknown'),
    'system':  GRY('System app'),
}

def scan_apps():
    """Scan apps and check versions. Returns (results, macos_info)."""
    macos_result = {}
    t = threading.Thread(target=lambda: macos_result.update(check_macos()), daemon=True)
    t.start()

    apps = find_apps()
    results = []
    for app in apps:
        bundle_id = app['bundle_id']

        # Skip noisy/irrelevant apps
        if app['name'] in HIDE_APP_NAMES or bundle_id in HIDE_BUNDLE_IDS:
            continue

        latest = cask_token = None

        homepage = ''

        if bundle_id in APPLE_SYSTEM_BUNDLES:
            # Apple's own apps: iTunes API returns nothing; they update via App Store
            source = 'Apple'

        elif bundle_id.startswith(MICROSOFT_BUNDLE_PREFIX):
            # Microsoft apps: always use Homebrew cask regardless of receipt.
            # Cask versions match CFBundleVersion (e.g. 16.114.26110224),
            # not CFBundleShortVersionString (e.g. 16.114.4) — same release,
            # different format. Use build_version for the comparison so we
            # don't report false "update available" when the app is current.
            if app.get('build_version'):
                app = {**app, 'version': app['build_version']}
            token = normalize_to_token(app['name'])
            cask  = fetch_cask(token)
            if cask:
                latest     = cask.get('version')
                homepage   = cask.get('homepage', '')
                source     = 'Homebrew Cask'
                cask_token = token
            else:
                source = 'Direct'

        elif is_app_store_app(app['path']):
            latest   = fetch_mas(bundle_id)
            homepage = f'https://itunes.apple.com/app?bundleId={bundle_id}'
            source   = 'App Store'

        else:
            token = normalize_to_token(app['name'])
            cask  = fetch_cask(token)
            if cask:
                latest     = cask.get('version')
                homepage   = cask.get('homepage', '')
                source     = 'Homebrew Cask'
                cask_token = token
            else:
                source   = 'Direct'
                homepage = HOMEPAGE_OVERRIDES.get(app['name'].lower(), '')

        time.sleep(0.05)
        results.append({
            'name':       app['name'],
            'installed':  app['version'],
            'bundle_id':  bundle_id,
            'path':       app['path'],
            'latest':     latest or '',
            'source':     source,
            'homepage':   homepage,
            'cask_token': cask_token or '',
        })

    t.join(timeout=40)
    for r in results:
        r['status'] = get_status(r)

    return results, macos_result

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
        print(BLD(f"\nMac App Checker  v{__version__}"))
        print(GRY("Checking macOS and scanning /Applications…\n"))

    results, macos_info = scan_apps()

    if json_mode:
        print(json.dumps({'macos': macos_info, 'apps': results}, indent=2))
        return

    if gui_mode:
        serve_dashboard(results, macos_info)
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

    W = 70
    print(BLD("UPDATES AVAILABLE"))
    print(GRY("─" * W))
    print(f"  {'#':<4}{'App':<28}{'Installed':<16}{'Latest'}")
    print(GRY("─" * W))
    for i, app in enumerate(needs, 1):
        cv = clean_ver(app.get('latest', '')) or '—'
        print(f"  {i:<4}{app['name']:<28}{app['installed']:<16}{cv:<16}  {STATUS_LABEL[app['status']]}")
    print(GRY("─" * W))

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
