#!/usr/bin/env python3
"""Fetch, verify and convert the deposit screen's classifier, Llama Prompt Guard 2 22M, for the backend image.

The model is gated on Hugging Face, so the platform's CI never downloads it: an operator fetches it once with their own
token into .deployment-assets/prompt-guard-2-22m/, and platform_bundle.py copies that folder into every bundle,
checked against services/backend/agent-runtime/prompt-guard-release.json. The image build verifies it again
(deposit_screen.verify_model) and never converts or downloads anything.

`fetch`:
  - reuses the folder when it already matches the lock;
  - otherwise downloads the pinned revision with HF_TOKEN (sent to huggingface.co only, never across a redirect to
    another host), refuses any file whose sha256 differs from the lock's source_files, converts it to ONNX with the
    lock's conversion command (optimum-cli, from a separate converter environment: torch and optimum never enter the
    image), and refuses a result whose files differ from the lock's files;
  - with `pin=True` (platform_bundle.py --pin-prompt-guard) and a lock with no revision, resolves the repository's
    current commit instead, and writes the revision and every sha256 into the lock for the operator to review and commit.
A lock without hashes is refused unless pinning was asked for.

The token is read by the caller from the environment and passed in; it is never written or printed.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request

HOST = 'huggingface.co'
# What the conversion needs from the repository: the weights, the model config and the tokenizer, and the licence
# texts that travel with the weights.
SOURCE_FILES = ('config.json', 'model.safetensors', 'tokenizer.json', 'tokenizer_config.json', 'special_tokens_map.json')
LICENCE = re.compile(r'^(?:LICEN[CS]E|USE_POLICY|NOTICE)(?:\.[A-Za-z]+)?$')
RUNTIME_FILES = ('config.json', 'model.onnx', 'tokenizer.json')
MAX_FILE_BYTES = 1_500_000_000


class FetchError(RuntimeError):
    """The model cannot be fetched or does not match its lock. The message says what to do."""


def sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def pinned(lock):
    files = lock.get('files') if isinstance(lock.get('files'), dict) else {}
    sources = lock.get('source_files') if isinstance(lock.get('source_files'), dict) else {}
    hexes = lambda values, size: all(isinstance(value, str) and re.fullmatch(f'[0-9a-f]{{{size}}}', value) for value in values)
    return (isinstance(lock.get('revision'), str) and re.fullmatch(r'[0-9a-f]{40}', lock['revision']) is not None
            and set(RUNTIME_FILES) <= set(files) and hexes(files.values(), 64) and bool(sources) and hexes(sources.values(), 64))


def matches(directory, files):
    """True when `directory` holds exactly the bytes `files` pins."""
    directory = Path(directory)
    return directory.is_dir() and all((directory / name).is_file() and not (directory / name).is_symlink()
                                      and sha256(directory / name) == digest for name, digest in files.items())


class SameHost(urllib.request.HTTPRedirectHandler):
    """Follow a redirect, but carry the token only to huggingface.co: file downloads redirect to a CDN."""
    def redirect_request(self, request, fp, code, message, headers, new_url):
        forwarded = super().redirect_request(request, fp, code, message, headers, new_url)
        if forwarded is not None and urllib.parse.urlsplit(new_url).hostname != HOST:
            forwarded.remove_header('Authorization')
        return forwarded


def http_get(url, token, target=None):
    """GET from huggingface.co with the token; bytes, or written to `target` when given."""
    if urllib.parse.urlsplit(url).hostname != HOST:
        raise FetchError('Refusing to send the Hugging Face token to ' + url)
    opener = urllib.request.build_opener(SameHost)
    request = urllib.request.Request(url, headers={'Authorization': 'Bearer ' + token, 'User-Agent': 'reveal-asset-fetch'})
    try:
        with opener.open(request, timeout=120) as response:
            if target is None:
                return response.read(10_000_000)
            size = 0
            with open(target, 'wb') as handle:
                for block in iter(lambda: response.read(1 << 20), b''):
                    size += len(block)
                    if size > MAX_FILE_BYTES:
                        raise FetchError(f'{url} is larger than expected')
                    handle.write(block)
    except urllib.error.HTTPError as error:
        if error.code in (401, 403):
            raise FetchError('Hugging Face refused the token: accept the model licence with the account that owns '
                             'HF_TOKEN, and use a token that can read gated repositories') from None
        raise FetchError(f'Hugging Face answered HTTP {error.code} for {urllib.parse.urlsplit(url).path}') from None
    return None


def resolve(repository, revision, token, get=http_get):
    """(commit sha, [file names]) of `revision` in the repository."""
    info = json.loads(get(f'https://{HOST}/api/models/{repository}/revision/{urllib.parse.quote(revision, safe="")}', token))
    sha = info.get('sha') if isinstance(info, dict) else None
    if not isinstance(sha, str) or not re.fullmatch(r'[0-9a-f]{40}', sha):
        raise FetchError(f'Hugging Face did not name a commit for {repository}@{revision}')
    return sha, sorted(item['rfilename'] for item in info.get('siblings', []) if isinstance(item, dict) and isinstance(item.get('rfilename'), str))


def convert(source, output, lock, converter=None):
    """Run the lock's conversion command (source and output substituted) with the converter on PATH."""
    command = [part.replace('{source}', str(source)).replace('{output}', str(output)) for part in lock['conversion']['command']]
    executable = converter or os.environ.get('REVEAL_PROMPT_GUARD_CONVERTER') or shutil.which(command[0])
    if not executable:
        raise FetchError(f'{command[0]} was not found. Install the converter in its own environment and set '
                         'REVEAL_PROMPT_GUARD_CONVERTER (see the lock file)')
    environment = {key: value for key, value in os.environ.items() if key != 'HF_TOKEN'}
    environment.update(HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1')   # convert the downloaded files, nothing else
    try:
        subprocess.run([executable, *command[1:]], check=True, capture_output=True, text=True, timeout=1800, env=environment)
    except subprocess.CalledProcessError as error:
        raise FetchError('The ONNX conversion failed: ' + (error.stderr or '').strip()[-800:]) from None
    try:
        version = subprocess.run([executable, '--version'], capture_output=True, text=True, timeout=60, env=environment).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        version = None
    return version or None


def fetch(destination, lock_path, *, token=None, pin=False, get=http_get, converter=None, run_conversion=convert):
    """Make `destination` hold the pinned model. Returns {'revision', 'files', 'fetched', 'pinned'}."""
    destination, lock_path = Path(destination), Path(lock_path)
    lock = json.loads(lock_path.read_text())
    if pinned(lock) and matches(destination, lock['files']):
        return {'revision': lock['revision'], 'files': lock['files'], 'fetched': False, 'pinned': False}
    if not pinned(lock) and not pin:
        raise FetchError(f'{lock_path.name} pins no revision or checksums. Accept the model licence, then pass '
                         '--pin-prompt-guard once to fetch, convert and pin it (see the lock file)')
    if not token:
        raise FetchError('Set HF_TOKEN to a Hugging Face read token whose account accepted the model licence')
    repository = lock['repository']
    pinning = not pinned(lock)
    revision, available = resolve(repository, lock['revision'] if not pinning else (lock.get('revision') or 'main'), token, get)
    if not pinning and revision != lock['revision']:
        raise FetchError('Hugging Face resolved a different commit than the lock pins')
    wanted = sorted(lock['source_files']) if not pinning else \
        [name for name in available if name in SOURCE_FILES or LICENCE.match(name)]
    missing = sorted(set(['config.json', 'model.safetensors']) - set(wanted))
    if missing:
        raise FetchError(f'{repository}@{revision} has no ' + ', '.join(missing))
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.prompt-guard-', dir=destination.parent) as temporary:
        work = Path(temporary)
        source, converted, runtime = work / 'source', work / 'onnx', work / 'runtime'
        source.mkdir(); runtime.mkdir()
        sources = {}
        for name in wanted:
            if '/' in name or name.startswith('.'):
                raise FetchError(f'Unexpected file name {name!r} in the repository listing')
            get(f'https://{HOST}/{repository}/resolve/{revision}/{urllib.parse.quote(name)}', token, source / name)
            sources[name] = sha256(source / name)
            if not pinning and sources[name] != lock['source_files'][name]:
                raise FetchError(f'{name} at {revision[:12]} differs from its pinned sha256; nothing was changed')
        version = run_conversion(source, converted, lock, converter)
        # The image carries the ONNX graph, the tokenizer the export wrote (the fast tokenizer), the model config and
        # the licence texts.
        for name in RUNTIME_FILES:
            origin = converted / name if (converted / name).is_file() else source / name
            if not origin.is_file():
                raise FetchError(f'The conversion left no {name}')
            shutil.copyfile(origin, runtime / name)
        for name in wanted:
            if LICENCE.match(name):
                shutil.copyfile(source / name, runtime / name)
        files = {path.name: sha256(path) for path in sorted(runtime.iterdir())}
        if not pinning and files != lock['files']:
            changed = sorted(name for name in set(files) | set(lock['files']) if files.get(name) != lock['files'].get(name))
            raise FetchError('The converted model differs from the pinned one (' + ', '.join(changed) + '). ONNX export is not '
                             'byte-reproducible across converter versions: restore the pinned folder, or re-pin')
        if destination.exists():
            shutil.rmtree(destination)
        runtime.rename(destination)
    if pinning:
        lock.update(revision=revision, source_files=sources, files=files)
        lock['conversion'] = {**lock['conversion'], 'converter': version}
        lock_path.write_text(json.dumps(lock, indent=2) + '\n')
    return {'revision': revision, 'files': files, 'fetched': True, 'pinned': pinning}


def copy_verified(source, target, lock_path):
    """Copy the model folder into a bundle after checking it against the lock. Returns the bundle's asset record."""
    lock = json.loads(Path(lock_path).read_text())
    if not pinned(lock):
        raise FetchError(f'{Path(lock_path).name} pins no revision or checksums. Fetch and pin the model first '
                         '(scripts/platform_bundle.py --fetch-assets --pin-prompt-guard; see the lock file)')
    if not matches(source, lock['files']):
        raise FetchError(f'{source} does not hold the pinned prompt-injection model. Fetch it with '
                         'HF_TOKEN=… scripts/platform_bundle.py --fetch-assets')
    Path(target).mkdir(parents=True)
    for name in lock['files']:
        shutil.copyfile(Path(source) / name, Path(target) / name)
    return {'repository': lock['repository'], 'revision': lock['revision'], 'files': dict(lock['files'])}
