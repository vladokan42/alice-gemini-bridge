#!/usr/bin/env python3
"""Fetch official stable releases; freeze identity only for this installation.

No mirrored service source, floating running image, silent old-version fallback,
background updater, credentials or live-device operations belong here.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parent.parent
BRIDGE_REPO = "skrashevich/quasar-bridge"
OMNI_REPO = "cheaper-inference/OmniRoute"
IMAGE_REPO = "diegosouzapw/omniroute"  # Published image named by the original project.
PATCH = ROOT / "patches/quasar-integration.patch"


class UpstreamError(ValueError):
    """Safe diagnostics only: never echo upstream response bodies."""


def run(command, cwd=None, env=None, timeout=180):
    try:
        result = subprocess.run(command, cwd=cwd, env=env, capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        raise UpstreamError("Upstream command unavailable/timed out: " + Path(command[0]).name) from None
    if result.returncode:
        raise UpstreamError("Upstream command failed: " + Path(command[0]).name + "; nothing installed, raw output hidden")
    return result.stdout


def github(path):
    request = urllib.request.Request("https://api.github.com/repos/" + path,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "alice-gemini-upstream-installer"})
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            raw = response.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise UpstreamError("GitHub response exceeds limit")
        return json.loads(raw)
    except (OSError, ValueError) as error:
        if isinstance(error, UpstreamError):
            raise
        raise UpstreamError("Official GitHub release lookup failed; retry later, no bundled fallback") from None


def release(repository, version="latest"):
    if repository not in (BRIDGE_REPO, OMNI_REPO):
        raise UpstreamError("Only original project repositories are allowed")
    if version != "latest" and not re.fullmatch(r"v?\d+\.\d+\.\d+", version):
        raise UpstreamError("Select latest stable or an exact stable release tag")
    endpoint = "latest" if version == "latest" else "tags/" + urllib.parse.quote(version, safe="")
    selected = github(repository + "/releases/" + endpoint)
    tag = selected.get("tag_name", "")
    if selected.get("draft") or selected.get("prerelease") or not re.fullmatch(r"v?\d+\.\d+\.\d+", tag):
        raise UpstreamError("Upstream did not return a stable release")
    if version != "latest" and tag != version:
        raise UpstreamError("Returned release differs from the exact requested tag; STOP")
    sha = github(repository + "/commits/" + tag).get("sha", "")
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise UpstreamError("Cannot resolve release to an exact source commit")
    return {"repository": repository, "tag": tag, "commit": sha}


def prepare_bridge(directory, version="latest"):
    directory = Path(directory).absolute()
    if directory.exists() or directory.is_symlink():
        raise FileExistsError("Upstream directory exists; inspect it or choose a new directory, never overlay")
    selected = release(BRIDGE_REPO, version)
    directory.mkdir(mode=0o700, parents=True)
    run(["git", "init", "--quiet", str(directory)])
    git_env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    run(["git", "-c", "core.hooksPath=/dev/null", "fetch", "--depth=1", "--no-tags",
         "https://github.com/" + BRIDGE_REPO + ".git", selected["commit"]], cwd=directory, env=git_env)
    run(["git", "-c", "core.hooksPath=/dev/null", "checkout", "--quiet", "--detach", "FETCH_HEAD"], cwd=directory)
    actual = run(["git", "rev-parse", "HEAD"], cwd=directory).decode().strip()
    if actual != selected["commit"]:
        raise UpstreamError("Downloaded bridge commit differs from selected release")
    try:
        run(["git", "apply", "--check", "--whitespace=error-all", str(PATCH)], cwd=directory)
    except UpstreamError:
        raise UpstreamError("Integration patch is incompatible with " + selected["tag"] + "; STOP, no old bridge fallback") from None
    run(["git", "apply", "--whitespace=error-all", str(PATCH)], cwd=directory)
    selected.update(patch_sha256=hashlib.sha256(PATCH.read_bytes()).hexdigest(), modified_build=True)
    return selected


def source_digest(directory):
    """Freeze the whole original module, not just today's package layout."""
    directory = Path(directory)
    digest = hashlib.sha256()
    for current, directories, files in os.walk(directory):
        directories[:] = sorted(name for name in directories if name != '.git')
        for name in directories + sorted(files):
            path = Path(current) / name
            if path.is_symlink():
                raise UpstreamError("Symlink source input is not supported")
        for name in sorted(files):
            path = Path(current) / name
            relative = str(path.relative_to(directory))
            if relative in ('integration-host-check', 'integration-receipt.json'):
                continue  # Only our own generated check artifacts, never source.
            digest.update(relative.encode() + b'\0' + path.read_bytes() + b'\0')
    return digest.hexdigest()


def module_environment(directory):
    """Use only this module and checksum-verified remote dependencies."""
    env = dict(os.environ, GOFLAGS='-mod=readonly', GOWORK='off', GOENV='off', GO111MODULE='on')
    module = json.loads(run(['go', 'mod', 'edit', '-json'], cwd=directory, env=env))
    if any(not item.get('New', {}).get('Version') for item in (module.get('Replace') or [])):
        raise UpstreamError('Unverified local replacement in go.mod; use an isolated original module')
    return env


def verify_bridge(directory, receipt):
    """Exercise upstream and patch tests plus this integration's real config/routes."""
    directory = Path(directory).absolute()
    original_inputs = source_digest(directory)
    env = dict(module_environment(directory), CGO_ENABLED="1")
    print("Checking original bridge + integration patch…", flush=True)
    run(["go", "test", "-race", "-count=1", "./..."], cwd=directory, env=env, timeout=600)
    run(["go", "vet", "./..."], cwd=directory, env=env, timeout=240)
    binary = directory / "integration-host-check"
    run(["go", "build", "-trimpath", "-o", str(binary), "./cmd/quasar-bridge"], cwd=directory, env=env, timeout=300)
    run([str(binary), "-config", str(ROOT / "config.example.json"), "-check-config"])
    cases = json.loads((ROOT / "examples/route-cases.json").read_text())
    for case in cases:
        raw = run([str(binary), "-config", str(ROOT / "config.example.json"), "-route", case["text"]])
        try:
            decision = json.loads(raw)
        except ValueError:
            raise UpstreamError("Bridge route response is not compatible JSON") from None
        if decision.get("route") != case["want"]:
            raise UpstreamError("Bridge route regression failed; no installation")
    if source_digest(directory) != original_inputs:
        raise UpstreamError("Bridge source changed during checks")
    receipt = dict(receipt, tests="race/vet/config/routes PASS", routing_cases=len(cases), source_sha256=original_inputs)
    path = directory / "integration-receipt.json"
    with path.open("x") as stream:
        json.dump(receipt, stream, indent=2)
        stream.write("\n")
    return receipt


def build_bridge(directory, output, arch=None):
    """Build only the verified source; no binary from our repository."""
    directory, output = Path(directory), Path(output)
    receipt = json.loads((directory / "integration-receipt.json").read_text())
    if (not isinstance(receipt, dict) or receipt.get('repository') != BRIDGE_REPO or
        receipt.get('modified_build') is not True or receipt.get('tests') != 'race/vet/config/routes PASS' or
        type(receipt.get('routing_cases')) is not int or
        receipt['routing_cases'] != len(json.loads((ROOT / 'examples/route-cases.json').read_text()))):
        raise UpstreamError('Invalid or unsuccessful bridge receipt; run fresh upstream checks')
    for field, pattern in (('tag', r'v?\d+\.\d+\.\d+'), ('commit', r'[0-9a-f]{40}'),
                           ('patch_sha256', r'[0-9a-f]{64}'), ('source_sha256', r'[0-9a-f]{64}')):
        value = receipt.get(field)
        if not isinstance(value, str) or not re.fullmatch(pattern, value):
            raise UpstreamError('Invalid bridge receipt identity; run fresh upstream checks')
    if receipt.get("patch_sha256") != hashlib.sha256(PATCH.read_bytes()).hexdigest():
        raise UpstreamError("Patch changed since upstream checks; use a fresh build directory")
    if receipt.get("source_sha256") != source_digest(directory):
        raise UpstreamError("Source changed since upstream checks; refusing stale receipt")
    if run(['git', 'rev-parse', 'HEAD'], cwd=directory).decode().strip() != receipt['commit']:
        raise UpstreamError('Bridge receipt commit differs from actual checkout; STOP')
    env = module_environment(directory)
    if arch:
        env.update(CGO_ENABLED="0", GOOS="linux", GOARCH=arch)
    run(["go", "build", "-trimpath", "-ldflags=-s -w", "-o", str(output.absolute()), "./cmd/quasar-bridge"],
        cwd=directory, env=env, timeout=600)
    return receipt


def omniroute(version="latest"):
    selected = release(OMNI_REPO, version)
    tag = selected["tag"].removeprefix("v")
    image = IMAGE_REPO + ":" + tag
    run(["docker", "pull", image], timeout=600)
    details = json.loads(run(["docker", "image", "inspect", image]))[0]
    digests = [s for s in details.get("RepoDigests", []) if valid_image(s)]
    identity = details.get("Id", "")
    if len(digests) != 1 or not re.fullmatch(r"sha256:[0-9a-f]{64}", identity):
        raise UpstreamError("Cannot freeze original image digest; refusing a floating tag")
    # Verify the actual pulled image, not a tag that might move in the meantime.
    actual = run(["docker", "run", "--rm", "--network=none", "--entrypoint", "node", identity,
                  "-p", "require('/app/package.json').version"], timeout=60).decode().strip()
    if actual != tag:
        raise UpstreamError("Original image version differs from GitHub stable release; STOP")
    return dict(selected, version=tag, image=digests[0], image_id=identity)


def valid_image(value):
    return isinstance(value, str) and bool(re.fullmatch(re.escape(IMAGE_REPO) + r"@sha256:[0-9a-f]{64}", value))


def bundle(directory, output):
    directory, output = Path(directory), Path(output)
    if output.exists() or output.is_symlink():
        raise FileExistsError("Bundle directory exists; inspect and move it first")
    output.mkdir(mode=0o700)
    (output / 'bin').mkdir()
    receipt = build_bridge(directory, output / 'bin/quasar-bridge', arch='arm64')
    for name in ('start.sh', 'supervise.sh', 'rollback-bind.sh', 'S98quasarbridge', 'install-device.sh', 'recovery.sh'):
        shutil.copyfile(ROOT / 'packaging' / name, output / name)
        (output / name).chmod(0o700)
    for name in ('LICENSE', 'NOTICE', 'THIRD_PARTY_NOTICES'):
        shutil.copyfile(directory / name, output / name)
    shutil.copyfile(ROOT / 'config.example.json', output / 'config.device.example.json')
    shutil.copyfile(ROOT / 'runtime.env.example', output / 'runtime.env.example')
    shutil.copyfile(ROOT / 'NOTICE', output / 'INTEGRATION-NOTICE')
    (output / 'upstream.json').write_text(json.dumps(receipt, indent=2) + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("bridge", "build", "bundle"))
    parser.add_argument("--directory", type=Path, default=ROOT / ".upstream/quasar-bridge")
    parser.add_argument("--version", default="latest")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        if args.command == 'bridge':
            receipt = verify_bridge(args.directory, prepare_bridge(args.directory, args.version))
            print(json.dumps(receipt, indent=2))
        elif args.command == 'build':
            output = args.output or ROOT / 'bin/quasar-bridge'
            output.parent.mkdir(parents=True, exist_ok=True)
            build_bridge(args.directory, output)
        else:
            bundle(args.directory, args.output or ROOT / 'dist')
        return 0
    except (ValueError, OSError, KeyError, TypeError) as error:
        message = str(error) if isinstance(error, UpstreamError) else type(error).__name__
        print("STOP: " + message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
