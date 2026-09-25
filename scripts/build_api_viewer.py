#!/usr/bin/env python3
"""Build offline Swagger views and a portable archive from validated artifacts."""
import hashlib
import json
from pathlib import Path
import zipfile
import shutil
from build_api_flow import main as build_flow

ROOT = Path(__file__).resolve().parents[1]
API = ROOT / 'api'


def main():
    spec = json.loads((API / 'openapi.json').read_text())
    report = json.loads((API / 'validation.json').read_text())
    assert report['passed'] and report['openapi_sha256'] == hashlib.sha256((API / 'openapi.json').read_bytes()).hexdigest(), 'Validate the current contract first'
    build_flow()
    manifest = json.loads((API / 'vendor/manifest.json').read_text())
    # Validate the vendored package bytes before embedding any executable assets.
    for name, digest in manifest['files'].items():
        assert hashlib.sha256((API / 'vendor' / name).read_bytes()).hexdigest() == digest, name
    data = {'REVEAL_SPEC': spec, 'REVEAL_EXCHANGES': json.loads((API / 'examples/exchanges.json').read_text()),
            'REVEAL_YAML': (API / 'openapi.yaml').read_text()}
    js = '\n'.join('window.' + k + '=' + json.dumps(v, ensure_ascii=False).replace('<', '\\u003c') + ';' for k, v in data.items())
    template = (ROOT / 'scripts/api-viewer.html').read_text()
    template = template.replace('<!-- REVEAL_DATA -->', '<script>' + js + '</script>')
    (API / 'index.html').write_text(template.replace('<!-- SWAGGER_CSS -->', '<link rel="stylesheet" href="vendor/swagger-ui.css">')
        .replace('<!-- SWAGGER_JS -->', '<script src="vendor/swagger-ui-bundle.js"></script>'))
    standalone = template.replace('<!-- SWAGGER_CSS -->', '<style>' + (API / 'vendor/swagger-ui.css').read_text() + '</style>')
    standalone = standalone.replace('<!-- SWAGGER_JS -->', '<script>' + (API / 'vendor/swagger-ui-bundle.js').read_text().replace('</script', '<\\/script') + '</script>')
    notices = {name: (API / 'vendor' / name).read_text() for name in ['LICENSE', 'NOTICE', 'swagger-ui-bundle.js.LICENSE.txt']}
    standalone = standalone.replace('</body>', '<script type="application/json" id="third-party-notices">' +
        json.dumps(notices).replace('<', '\\u003c') + '</script>\n</body>')
    (API / 'reveal-api.html').write_text(standalone)
    target = API / 'reveal-openapi-handoff.zip'
    mirror = API / 'reveal-openapi'
    files = sorted(p for p in API.rglob('*') if p.is_file() and p != target and p.suffix != '.png'
                   and mirror not in p.parents and API / '.reveal-openapi-staging' not in p.parents
                   and p.name != 'browser-review.json')
    # Refresh the extracted handoff without ever nesting its previous copy in the zip.
    staging = API / '.reveal-openapi-staging'
    if staging.exists(): shutil.rmtree(staging)
    staging.mkdir()
    for path in files:
        dest = staging / path.relative_to(API)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, dest)
    if mirror.exists(): shutil.rmtree(mirror)
    staging.rename(mirror)
    with zipfile.ZipFile(target, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in files:
            name = 'reveal-openapi/' + str(path.relative_to(API))
            info = zipfile.ZipInfo(name, date_time=(2026, 9, 24, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, path.read_bytes())
    print('Built api/index.html, standalone api/reveal-api.html, and api/reveal-openapi-handoff.zip')


if __name__ == '__main__':
    main()
