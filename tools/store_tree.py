# -*- coding: utf-8 -*-
"""store_tree.py - writes apps/overboost/, the SOURCE PACKAGE the Store builds on the head unit (mgu-store docs/PUBLISHING.md, "Repo tree").

Reads this repository's src/ and app/ (the sources of OVERBOOST for iDrive 8) and writes

    apps/overboost/mgu-app.json   {"schema":1, id, version, tree:"package", modules[], sdk[]}
    apps/overboost/src/*.js       the app's modules, flattened (require('./name.js')), entry first
    apps/overboost/package/       the staged package tree: package.json, manifest.json, permissions.json, rhmiData/, ...
                                  (no javascript/app.js: the head unit writes it)

The head unit (mgu-deploy) links the modules against the oap-sdk of a stock BMW app on the unit (its "donor") and installs the package.
No BMW file is in this repository. The fault-text database (src/dtc_db.pack) is BMW's and is not published: without it in src/, the
Fault Scanner runs on its built-in texts.

    python3 tools/store_tree.py            # rewrite apps/overboost/ from src/ and app/
    python3 tools/store_tree.py --check    # build into a temporary folder and compare; exit 1 when apps/overboost/ is out of date

Raise "version" in app/version.json for every release (the Store updates by version), run this, commit apps/overboost/ with the rest.
Needs Python 3 and Node.js (syntax check only).
"""
import argparse
import collections
import json
import os
import re
import subprocess
import sys
import filecmp
import shutil
import tarfile
import tempfile
import uuid

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = HERE
CODE = os.path.join(SRC, 'src')
APPDIR = os.path.join(SRC, 'app')
OUT = os.path.join(HERE, 'apps', 'overboost')

NS = uuid.UUID('6b1d3a52-8f0e-4c4b-9a77-0d0c4d4e5f10')
APP_ID = 'overboost'
LOGGING_ID = 'OAB0'
LANGUAGES = ('ar_AE cs_CZ da_DK de_DE el_GR en_GB en_US es_ES fi_FI fr_FR he_IL hu_HU id_ID it_IT ja_JP ko_KR nb_NO nl_NL no_NO pl_PL '
             'pt_PT pt_BR ro_RO ru_RU sk_SK sl_SI sv_SE th_TH tr_TR uk_UA vi_VN zh_CN zh_TW').split()
NATIVES = ('certmgr', 'codings', 'ecu', 'nare_logging', 'necemapaths', 'personalization', 'smrf-native-cpp', 'someip', 'wscpp-client')
# locul intrării în meniu: aceleași valori ca la aplicațiile IterioDev doar pentru iDrive 8 (iteriodev_draggy / cardash)
ENTRY_STATE_ID = 3000
ENTRY_FOCUS_ID = 0
REQUIRES = {'app-privacy': '1.7.1', 'mgu-deploy': '0.7.5', 'mgu-render': '0.6.9'}
# numele din oap-sdk pe care donorul trebuie să le rezolve (aceleași trei ca la OVERBOOST iDrive 7 și la aplicațiile IterioDev)
SDK_NAMES = ['oap-sdk/src/core/OnlineApp.js', 'oap-sdk/src/logging/logging', 'oap-sdk/src/someip/someipBuilders']

# ---- aplatizarea codului (aceeași ca la OVERBOOST iDrive 7)
ALIAS = {'pack.js': 'kit/pack.js', 'diag.js': 'kit/diag.js', 'known_texts.js': 'apps/dtc_scanner/known_texts.js'}
DROP = {'update.js', 'loader.js', 'apps/update/main.js'}
BUILTIN = {'fs', 'path', 'zlib', 'events', 'net', 'os', 'crypto', 'child_process', 'dgram', 'http', 'https', 'url', 'util', 'module'}
STATIC_JSON = ['kit/locale.json', 'kit/fonts/light.json', 'kit/fonts/gear.json', 'kit/fonts/bold.json', 'kit/fonts/label.json', 'kit/fonts/num.json']
APP_IDS = ['menu', 'car_vitals', 'draggy', 'dtc_scanner', 'engine']

SHIM = """
// ---- adăugat de toolkit (iDrive 8): modulele native ale unității. Vechiul kit le lega în node_modules; aici, dacă require() nu le găsește,
// ---- se încarcă direct din /usr/lib/<nume>.node. Dacă se găsesc și fără ajutor, nu se întâmplă nimic.
(function () {
    const M = require('module'), fsx = require('fs');
    const NAT = %s;
    const orig = M._resolveFilename;
    M._resolveFilename = function (request, parent, isMain, options) {
        try { return orig.call(this, request, parent, isMain, options); } catch (e) {
            if (NAT.indexOf(request) >= 0 && fsx.existsSync('/usr/lib/' + request + '.node')) return '/usr/lib/' + request + '.node';
            throw e;
        }
    };
})();
""" % json.dumps(list(NATIVES))


def flat(rel):
    rel = ALIAS.get(rel, rel)
    if rel == 'main.js':
        return 'ob_main.js'
    base = 'kit_' + rel[4:] if rel.startswith('kit/') else 'app_' + rel[5:] if rel.startswith('apps/') else 'ob_' + rel
    if rel.endswith('.json'):
        return base.replace('/', '_').replace('.', '_') + '.js'
    return base.replace('/', '_')


def resolve(frm, name):
    d = os.path.dirname(frm)
    p = os.path.normpath(os.path.join(d, name)).replace('\\', '/')
    for c in (p, p + '.js', p + '.json', p + '/index.js'):
        if os.path.isfile(os.path.join(CODE, c)):
            return c
    return None


def patch(text, old, new, what):
    if text.count(old) != 1:
        raise ValueError('petic "%s": se aștepta exact 1 potrivire, sunt %d' % (what, text.count(old)))
    return text.replace(old, new)


MENU_UPDATE = "                { t: upd.st.found && upd.st.found.newer ? 'Update   (new version ' + upd.st.found.tag + ')' : 'Update', f: () => go('UPDATE') },\n"

PATCHES = {
    'main.js': [
        ("'use strict';\n", "'use strict';\n" + SHIM, 'native'),
        ("const { OnlineApp } = require('oap-sdk');", "const OnlineApp = (function (m) { return m.OnlineApp || m; })(require('oap-sdk/src/core/OnlineApp.js'));", 'sdk'),
        ("require('./update').create(L)", "require('./ob_update.js').create(L)", 'update stub'),
        (", UPDATE: 'update' };", " };", 'fără aplicația update'),
        (MENU_UPDATE, '', 'fără butonul Update'),
    ],
    'host8.js': [
        ("        const dir = path.join(__dirname, 'apps', id);\n        const spec = JSON.parse(fs.readFileSync(path.join(dir, 'app.json'), 'utf8'));",
         "        const reg = require('./ob_apps.js')[id];\n        if (!reg) throw new Error('no app ' + id);\n        const spec = JSON.parse(JSON.stringify(reg.spec));", 'registru'),
        ("        require(path.join(dir, 'main.js'));", "        reg.load();", 'încărcare'),
        ("{ label: 'Engine Config', screen: 'ENGINE' }].concat(require('./kit/hmi').id7 ? [] : [{ label: 'Update', screen: 'UPDATE' }]),",
         "{ label: 'Engine Config', screen: 'ENGINE' }],", 'chips'),
    ],
    'signals.js': [
        ("try { someip = require('someip'); }", "try { someip = require(['some', 'ip'].join('')); }", 'someip (nativ, găsit de unitate)'),
    ],
    'faults.js': [
        ("path.join(process.env.OVERBOOST_SRC || __dirname, 'dtc_db')", "path.join(__dirname, 'dtc_db')", 'db'),
        ("OWN = require(path.join(process.env.OVERBOOST_SRC || __dirname, 'known_texts'));", "OWN = require('./known_texts');", 'texte'),
    ],
    'apps/dtc_scanner/main.js': [
        ("path.join(process.env.OVERBOOST_SRC || path.join(__dirname, '..', '..'), 'dtc_db')", "path.join(__dirname, 'dtc_db')", 'db'),
        ("Object.assign({}, req('./known_texts'), req('./dtc_texts'))", "Object.assign({}, require('./known_texts'))", 'texte'),
    ],
    'apps/dtc_scanner/diagnostic-text.js': [
        ("language === 'zh-CN' ? require('../../kit/pack').open(path.join(__dirname, 'diagnostic_i18n')) : null", "null", 'fără baza zh'),
    ],
    'kit/brand.js': [
        ("path.join(process.env.OVERBOOST_SRC || path.join(__dirname, '..'), 'logo.rgba')", "path.join(__dirname, 'logo.rgba')", 'logo'),
    ],
    'kit/fonts.js': [
        ("const j = JSON.parse(fs.readFileSync(path.join(DIR, name + '.json'), 'utf8'));", "const j = require('./kit_fonts_data.js')[name]();", 'fonturi'),
    ],
}

GENERATED = {'./ob_update.js', './ob_apps.js', './kit_fonts_data.js'}
GEN_HMI = "'use strict';\nmodule.exports = { id: 'ID8', id7: false, id8: true };\n"
GEN_UPDATE = """'use strict';
/* fără actualizator în mașină: actualizările se instalează cu mgu-deploy (aceeași cale ca aplicațiile IterioDev) */
const VERSION = %s;
exports.create = () => ({
    base: { version: VERSION, base: 3 }, st: { found: null, msg: '', busy: false },
    cur: () => null, check: () => Promise.resolve(), install: () => Promise.resolve(), remove: () => {},
});
"""


def _read_json(*parts):
    with open(os.path.join(*parts), encoding='utf-8') as fh:
        return json.load(fh)


def _read(*parts):
    with open(os.path.join(*parts), 'rb') as fh:
        return fh.read()


def _dump(data):
    return (json.dumps(data, indent=2, ensure_ascii=False) + '\n').encode('utf-8')


def _uid(kind):
    return str(uuid.uuid5(NS, APP_ID + ':' + kind))


def _acl(domain, iface):
    return {'_typeName': 'joynr.infrastructure.DacTypes.MasterAccessControlEntry', 'uid': '${APPNAME}', 'domain': domain,
            'interfaceName': iface, 'defaultRequiredTrustLevel': 'HIGH', 'possibleRequiredTrustLevels': [],
            'defaultRequiredControlEntryChangeTrustLevel': 'HIGH', 'possibleRequiredControlEntryChangeTrustLevels': [],
            'operation': '*', 'defaultConsumerPermission': 'YES', 'possibleConsumerPermissions': []}


# Tabelele de acces ale aplicațiilor IterioDev pentru iDrive 8 (aceleași 9 + 1 intrări în toate; numele interfețelor sunt cele din MainApp-21,
# cu litere mici la appstatus / privateentrypoint, exact ca în pachetele lor).
ACL = [
    ('hmi.entrypoint2', 'de/bmw/infotainment/hmi/entrypoint2/EntryPointManager2'),
    ('hmi.resourcemanagement2', 'de/bmw/infotainment/hmi/resourcemanagement2/ResourceManager2'),
    ('hmi.resourcemanagement2', 'de/bmw/infotainment/hmi/resourcemanagement2/StringBundle2'),
    ('hmi.usecasemanagement', 'de/bmw/infotainment/hmi/usecasemanagement/UseCaseContext'),
    ('telematic.oapcontroller', 'de/bmw/infotainment/telematic/oapcontroller/OAPController'),
    ('de.bmw.infotainment.telematic.oap.oapcontroller', 'de/bmw/infotainment/telematic/oapcontroller/OAPController'),
    ('de.bmw.infotainment.telematic.oap.appStatus_${APPNAME}', 'de/bmw/infotainment/telematic/oap/appstatus/AppStatus'),
    ('de.bmw.infotainment.telematic.oap.appstart', 'de/bmw/infotainment/telematic/oap/appstart/AppStart'),
    ('de.bmw.infotainment.telematic.oap.privateEntryPoint_${APPNAME}', 'de/bmw/infotainment/telematic/oap/privateentrypoint/PrivateEntryPoint'),
]


def permissions():
    return {
        'masterAccessTable': [_acl(d, i) for d, i in ACL],
        'masterRegistrationTable': [
            {'_typeName': 'joynr.infrastructure.DacTypes.MasterRegistrationControlEntry', 'uid': '${APPNAME}',
             'domain': 'de.bmw.infotainment.telematic.oap.heartbeat_${APPNAME}',
             'interfaceName': 'de/bmw/infotainment/telematic/oap/heartbeat/HeartbeatMonitor',
             'defaultRequiredTrustLevel': 'HIGH', 'possibleRequiredTrustLevels': [],
             'defaultRequiredControlEntryChangeTrustLevel': 'HIGH', 'possibleRequiredControlEntryChangeTrustLevels': [],
             'defaultProviderPermission': 'YES', 'possibleProviderPermissions': []}],
    }


# containerul logic cu „bookmark”, prezent în uiDescription și application.xml ale TUTUROR aplicațiilor IterioDev pentru iDrive 8
BOOKMARK_XML = ('    <container id="1000" type="logical">\n        <layoutGroup id="1100">\n'
                '            <component id="1110" type="bookmark"/>\n        </layoutGroup>\n    </container>\n')


def with_bookmark(xml_text, ui):
    if 'type="bookmark"' not in xml_text:
        if '<application>\n' not in xml_text:
            raise ValueError('application.xml: nu găsesc <application>')
        xml_text = xml_text.replace('<application>\n', '<application>\n' + BOOKMARK_XML, 1)
    containers = ui['containers']
    if '1000' not in containers:
        for k in list(containers):
            if k in ('1000', '1100', '1110'):
                raise ValueError('uiDescription: id %s ocupat' % k)
        ui['containers'] = collections.OrderedDict(
            [('1000', {'type': 'logical', 'name': 'logicalContainer', 'components': {'1110': {'type': 'bookmark', 'name': 'bookmark'}}})]
            + list(containers.items()))
    return xml_text, ui


def make_tree(tree, ver, spec):
    """Arborele pachetului (fără javascript/app.js, pe care îl scrie mgu-deploy)."""
    def put(rel, data):
        p = os.path.join(tree, *rel.split('/'))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, 'wb') as fh:
            fh.write(data if isinstance(data, bytes) else data.encode('utf-8'))
    manifest = _read_json(APPDIR, 'manifest.json')
    watch = _read_json(APPDIR, 'watch.json')
    for iface, entry in watch.items():
        manifest['proxies'][iface] = '*' if entry.get('all') else entry
    put('manifest.json', _dump(manifest))
    put('permissions.json', _dump(permissions()))
    put('npm-shrinkwrap.json', _dump({'name': APP_ID, 'version': ver, 'lockfileVersion': 1, 'requires': True, 'dependencies': {}}))
    put('icon.png', _read(APPDIR, 'icon.png'))
    for data in ('dtc_db.pack', 'logo.rgba'):
        if data == 'dtc_db.pack' and not os.path.isfile(os.path.join(CODE, data)):
            continue                      # BMW's fault-text database: not published; the Fault Scanner then uses its built-in texts
        put('javascript/' + data, _read(CODE, data))
    ui_dir = 'rhmiData/uiDescriptions/ID8/'
    xml_text, ui = with_bookmark(_read(APPDIR, 'application.xml').decode('utf-8'),
                                 json.loads(_read(APPDIR, 'uiDescription.json').decode('utf-8'), object_pairs_hook=collections.OrderedDict))
    put(ui_dir + 'application.xml', xml_text)
    put(ui_dir + 'uiDescription.json', _dump(ui))
    strings = _read(APPDIR, 'strings.json')
    for lang in LANGUAGES:
        put('rhmiData/strings/%s.json' % lang, strings)
        put('rhmiData/variantGroups/Common/texts/%s.json' % lang, strings)
    icon_name = APP_ID + '_icon.png'
    put('rhmiData/resources/' + icon_name, _read(APPDIR, 'icon.png'))
    put('rhmiData/resources/overboost_logo.png', _read(APPDIR, 'logo.png'))
    put('rhmiData/application.json', _dump({
        'schemaVersion': '1.0.0',
        'application': {
            'uuid': _uid('app'), 'type': 'OnlineApp', 'name': APP_ID, 'vendor': 'custom', 'version': '1.0.0',
            'defaultLanguage': 'en_GB',
            'uiDescriptions': [{'applicationFilePath': './uiDescriptions/ID8/application.xml',
                                'uiDescriptionFilePath': './uiDescriptions/ID8/uiDescription.json', 'hmi': ['ID8']}],
            'stringBundleUuid': _uid('strings'), 'resourceBundleUuid': _uid('resources'),
            'variantGroups': [{'stringsPath': './variantGroups/Common/texts', 'hmi': ['ID8'], 'brands': ['BMW'], 'resourcesPath': './resources'}],
            'entryPoints': [{'uuid': _uid('entry'), 'textId': 'SID_ID8_BENCH_DEMO_TITLE', 'insertionStateId': ENTRY_STATE_ID,
                             'insertionFocusId': ENTRY_FOCUS_ID, 'appearance': 'DEFAULT', 'insertionMode': 0, 'insertionOrder': 0,
                             'insertionPriority': 0, 'insertionGroup': 0, 'isGroupHeader': False, 'availability': 'ENABLED',
                             'icon': icon_name}],
            'appletId': spec['appletId'],
        }}))
    pkg = collections.OrderedDict([
        ('name', APP_ID), ('version', ver), ('description', 'OVERBOOST (iDrive 8): M Vitals, Drag Timer, Fault Scanner, Engine Config'),
        ('main', 'javascript/runner.js'), ('private', False), ('author', 'OVERBOOST'),
        ('mguHmi', ['ID8']), ('mguRequires', dict(REQUIRES)),
        ('oapAppSettings', collections.OrderedDict([
            ('maxDiskUsage', 20971520), ('cpuLimit', 60), ('memoryThresholdKB', 196608), ('priority', 'PRIO_LOW'),
            ('visibility', 'FOREGROUND'), ('latencyClass', 'FOREGROUND'), ('loggingId', LOGGING_ID), ('userLoginRequired', False),
            ('backgroundPriority', 200), ('someipManifest', 'manifest.json')])),
        ('files', ['icon.png', 'javascript', 'manifest.json', 'package.json', 'permissions.json', 'rhmiData', 'npm-shrinkwrap.json']),
        ('dependencies', {'oap-sdk': '24.10.2'}), ('mguLocalOnly', True), ('productName', spec['title']),
    ])
    put('package.json', _dump(pkg))


def flatten(app_dir, ver):
    """Modulele aplicației -> app_dir/src/*.js ; întoarce lista numelor (intrarea prima)."""
    seen = collections.OrderedDict()
    rx = re.compile(r"require\('([^']+)'\)")
    work = ['main.js', 'host8.js', 'signals.js', 'faults.js', 'engine.js', 'vitals.js', 'drag.js', 'ui.js', 'timer.js'] + \
           ['apps/%s/main.js' % a for a in APP_IDS] + ['apps/dtc_scanner/known_texts.js', 'kit/fonts.js', 'kit/brand.js']
    while work:
        rel = work.pop(0)
        rel = ALIAS.get(rel, rel)
        if rel in seen or rel in DROP:
            continue
        text = open(os.path.join(CODE, rel), encoding='utf-8').read()
        for old, new, what in PATCHES.get(rel, []):
            text = patch(text, old, new, rel + ': ' + what)
        # comentariile care citează require('...') ar fi citite de linker ca require-uri reale
        text = '\n'.join(l.replace("require('", 'require("') if re.match(r'\s*(\*|//|/\*)', l) else l for l in text.split('\n'))
        seen[rel] = text
        for name in rx.findall(text):
            if name.startswith('.') and name not in GENERATED:
                tgt = resolve(rel, name)
                if tgt is None:
                    raise ValueError('%s cere %s: nu există' % (rel, name))
                if tgt.endswith('.json') or tgt in seen:
                    continue
                work.append(tgt)
    names = {}
    for rel in seen:
        n = flat(rel)
        if n in names.values():
            raise ValueError('două module cu același nume: ' + n)
        names[rel] = n
    mods = []
    for rel, text in seen.items():
        def sub(m, rel=rel):
            name = m.group(1)
            if not name.startswith('.') or name in GENERATED:
                return m.group(0)
            tgt = resolve(rel, name)
            if tgt.endswith('.json'):
                return "require('./%s')" % flat(tgt)
            return "require('./%s')" % names[ALIAS.get(tgt, tgt)]
        mods.append((names[rel], rx.sub(sub, text)))
    gen = {'kit_hmi.js': GEN_HMI, 'ob_update.js': GEN_UPDATE % json.dumps(ver)}
    for j in STATIC_JSON:
        gen[flat(j)] = 'module.exports = %s;\n' % open(os.path.join(CODE, j), encoding='utf-8').read().strip()
    gen['kit_fonts_data.js'] = "'use strict';\nmodule.exports = {\n" + ''.join(
        "    %s: () => require('./%s'),\n" % (f, flat('kit/fonts/%s.json' % f)) for f in ('light', 'gear', 'bold', 'label', 'num')) + "};\n"
    reg = ["'use strict';", 'module.exports = {']
    for a in APP_IDS:
        spec = _read_json(CODE, 'apps', a, 'app.json')
        reg.append("    %s: { spec: %s, load: () => require('./%s') }," % (json.dumps(a), json.dumps(spec), names['apps/%s/main.js' % a]))
    reg.append('};\n')
    gen['ob_apps.js'] = '\n'.join(reg)
    mods = [(n, t) for n, t in mods if n != 'kit_hmi.js'] + list(gen.items())
    mods.sort(key=lambda e: (e[0] != 'ob_main.js', e[0]))
    os.makedirs(os.path.join(app_dir, 'src'))
    for n, t in mods:
        with open(os.path.join(app_dir, 'src', n), 'w', encoding='utf-8') as fh:
            fh.write(t)
    return [n for n, _ in mods]


def write(app_dir):
    """Writes the source package into app_dir (made here); returns (version, module names)."""
    spec = _read_json(APPDIR, 'app.json')
    ver = str(_read_json(APPDIR, 'version.json')['version'])
    os.makedirs(app_dir)
    make_tree(os.path.join(app_dir, 'package'), ver, spec)
    names = flatten(app_dir, ver)
    with open(os.path.join(app_dir, 'mgu-app.json'), 'w', encoding='utf-8', newline='\n') as fh:
        json.dump({'schema': 1, 'id': APP_ID, 'version': ver, 'tree': 'package', 'modules': ['src/' + n for n in names], 'sdk': SDK_NAMES}, fh, indent=1)
        fh.write('\n')
    return ver, names


def verify(tgz, node='node'):
    """Verificări locale ale unui pachet construit; întoarce lista de probleme (goală = în regulă)."""
    bad = []
    with tarfile.open(tgz, 'r:gz') as t:
        mem = {m.name: m for m in t.getmembers()}
        for n in mem:
            if n.startswith('/') or '..' in n.split('/'):
                bad.append('cale nesigură: ' + n)
            if os.path.basename(n) in ('mgu.sh', 'apply-hook.sh', 'patch-mainapp.js', 'reload-mainapp.sh', 'gen-interfaces.js', 'loader.js', 'update.js'):
                bad.append('fișier al vechiului kit / actualizator în pachet: ' + n)
        if 'mgu-app.json' not in mem:
            return ['lipsește mgu-app.json (nu e pachet sursă)']
        m = json.load(t.extractfile('mgu-app.json'))
        if m.get('schema') != 1 or m.get('id') != APP_ID:
            bad.append('mgu-app.json: schema/id greșite')
        if not os.path.basename(tgz).startswith('%s@%s.' % (APP_ID, m.get('version'))):
            bad.append('numele fișierului nu corespunde versiunii')
        mods = m.get('modules') or []
        if not mods or mods[0] != 'src/ob_main.js':
            bad.append('mgu-app.json: intrarea trebuie să fie prima (src/ob_main.js)')
        for f in mods:
            if f not in mem:
                bad.append('modul lipsă: ' + f)
        if not m.get('sdk'):
            bad.append('mgu-app.json: lipsește lista sdk')
        tree = m.get('tree', '')
        for must in ('package.json', 'manifest.json', 'permissions.json', 'npm-shrinkwrap.json', 'rhmiData/application.json',
                     'rhmiData/uiDescriptions/ID8/application.xml', 'rhmiData/uiDescriptions/ID8/uiDescription.json', 'icon.png'):
            if '%s/%s' % (tree, must) not in mem:
                bad.append('lipsește %s/%s' % (tree, must))
        if bad:
            return bad
        if any(n.endswith('javascript/app.js') for n in mem):
            bad.append('pachetul sursă nu trebuie să conțină javascript/app.js (îl scrie mgu-deploy)')
        pkg = json.load(t.extractfile('%s/package.json' % tree))
        if pkg.get('name') != APP_ID or pkg.get('version') != m.get('version') or pkg.get('mguLocalOnly') is not True:
            bad.append('package.json: nume / versiune / mguLocalOnly')
        if pkg.get('mguHmi') != ['ID8']:
            bad.append('package.json: mguHmi trebuie să fie exact ["ID8"], e %r' % (pkg.get('mguHmi'),))
        if pkg.get('main') != 'javascript/runner.js':
            bad.append('package.json: main trebuie să fie javascript/runner.js (punctul de intrare al donorului)')
        app = json.load(t.extractfile('%s/rhmiData/application.json' % tree))['application']
        if [u.get('hmi') for u in app.get('uiDescriptions', [])] != [['ID8']]:
            bad.append('rhmiData: uiDescriptions trebuie să fie doar pentru ID8')
        if app.get('name') != APP_ID or not app.get('entryPoints'):
            bad.append('rhmiData/application.json: nume sau intrare lipsă')
        perms = json.load(t.extractfile('%s/permissions.json' % tree))
        got = {(e['domain'], e['interfaceName']) for e in perms.get('masterAccessTable', [])}
        if got != set(ACL) or not perms.get('masterRegistrationTable'):
            bad.append('permissions.json: nu are exact tabelele de acces ale aplicațiilor IterioDev')
        man = json.load(t.extractfile('%s/manifest.json' % tree))
        if 'de.bmw.infotainment.hmi.entrypoint2.EntryPointManager2' not in man.get('proxies', {}):
            bad.append('manifest.json: lipsește EntryPointManager2')
        # modulele: sintaxă, require-uri doar între module, fără oap-sdk gol
        with tempfile.TemporaryDirectory(prefix='ob-verify-') as tmp:
            try:
                t.extractall(tmp, filter='data')
            except TypeError:
                t.extractall(tmp)
            have = {os.path.basename(f) for f in mods}
            rx = re.compile(r"require\('([^']+)'\)")
            for f in mods:
                p = os.path.join(tmp, f)
                text = open(p, encoding='utf-8').read()
                for name in rx.findall(text):
                    if name.startswith('.') and os.path.basename(name) not in have:
                        bad.append('%s cere %s: nu e modul al pachetului' % (f, name))
                    elif not name.startswith('.') and name not in BUILTIN and name not in SDK_NAMES:
                        bad.append('%s cere "%s": nici modul, nici builtin, nici nume sdk' % (f, name))
                r = subprocess.run([node, '--check', p], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                if r.returncode:
                    bad.append('sintaxă %s: %s' % (f, r.stderr.decode('utf-8', 'replace').strip()[:160]))
    return bad


def same(a, b):
    """True when the two folders hold the same files with the same bytes."""
    cmp = filecmp.dircmp(a, b)
    if cmp.left_only or cmp.right_only or cmp.funny_files:
        return False
    if filecmp.cmpfiles(a, b, cmp.common_files, shallow=False)[1:] != ([], []):
        return False
    return all(same(os.path.join(a, d), os.path.join(b, d)) for d in cmp.common_dirs)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--check', action='store_true', help='compare with apps/overboost/ instead of writing it')
    a = ap.parse_args()
    with tempfile.TemporaryDirectory(prefix='overboost-store-') as tmp:
        built = os.path.join(tmp, 'overboost')
        ver, names = write(built)
        tgz = os.path.join(tmp, '%s@%s.tgz' % (APP_ID, ver))
        with tarfile.open(tgz, 'w:gz') as t:
            t.add(built, arcname='.')
        # verify() reads the members by their names, so pack them without the leading "./"
        with tarfile.open(tgz, 'w:gz') as t:
            for n in sorted(os.listdir(built)):
                t.add(os.path.join(built, n), arcname=n)
        bad = verify(tgz)
        if bad:
            print('PROBLEMS:\n  ' + '\n  '.join(bad))
            sys.exit(1)
        if a.check:
            if not os.path.isdir(OUT) or not same(built, OUT):
                print('apps/overboost/ is out of date: run python3 tools/store_tree.py')
                sys.exit(1)
            print('OK: apps/overboost/ matches src/ and app/ (version %s)' % ver)
            return
        if os.path.isdir(OUT):
            shutil.rmtree(OUT)
        os.makedirs(os.path.dirname(OUT), exist_ok=True)
        shutil.copytree(built, OUT)
    print('apps/overboost/ written: version %s, %d modules' % (ver, len(names)))


if __name__ == '__main__':
    main()
