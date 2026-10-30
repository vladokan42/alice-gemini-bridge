#!/usr/bin/env python3
"""Fresh OmniRoute + rooted ARM64 Station setup. Python stdlib only.

Never prints keys, provider responses, firmware config or subprocess stderr.
"""
import argparse
import getpass
import hashlib
import http.cookiejar
import io
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
import upstream
COMBO = "alice-gemini"
NETWORKS = tuple(ipaddress.ip_network(value) for value in ((0x0A000000, 8), (0xAC100000, 12), (0xC0A80000, 16)))
DEVICE_STATE = "/data/opt/quasar-bridge"


class SetupError(ValueError, RuntimeError):
    """Only explicit, secret-free diagnostic text may reach the console."""


def lan_ip(value):
    try:
        address = ipaddress.IPv4Address(value)
    except ipaddress.AddressValueError:
        raise SetupError("Use a specific private IPv4 address, not a hostname/wildcard") from None
    if not (address.is_loopback or any(address in n for n in NETWORKS)):
        raise SetupError("Public/wildcard bind is not supported; use a private LAN/VPN IPv4")
    return str(address)


def validate_endpoint(value):
    url = urllib.parse.urlsplit(value)
    if url.scheme not in ("http", "https") or not url.hostname or url.username is not None or url.password is not None or url.query or url.fragment:
        raise SetupError("Endpoint must be HTTP(S), without credentials/query/fragment")
    if url.scheme == "http":
        lan_ip(url.hostname)
    if url.path.rstrip("/") != "/v1":
        raise SetupError("OmniRoute endpoint must end in /v1")
    if url.port is not None and not 1 <= url.port <= 65535:
        raise SetupError("Invalid endpoint port")
    return value.rstrip("/")


def model_id(value):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}", value) or ".." in value:
        raise SetupError("Use the exact model ID, without whitespace or shell syntax")
    return value


def ssh_target(value):
    if not re.fullmatch(r"(?:[A-Za-z0-9_][A-Za-z0-9_.-]*@)?[A-Za-z0-9][A-Za-z0-9_.-]*", value):
        raise SetupError("Use a known SSH alias or user@host, without options or shell syntax")
    return value


def create_state(path):
    path = Path(path)
    if path.exists() or path.is_symlink():
        raise FileExistsError("Installation directory exists; refusing to overwrite")
    path.mkdir(mode=0o700, parents=False)


def write_private(path, value):
    raw = json.dumps(value, ensure_ascii=False, indent=2) + "\n" if not isinstance(value, bytes) else value
    data = raw.encode() if isinstance(raw, str) else raw
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)


def read_secret(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077 or path.stat().st_uid != os.geteuid():
        raise SetupError("Secret file must be owned by this user, regular and mode 0600")
    value = path.read_text().strip()
    if not value or len(value) > 10000 or any(c in value for c in "\r\n\0"):
        raise SetupError("Secret must be a nonempty single line")
    return value


def run(command, data=None, timeout=120, cwd=None):
    try:
        result = subprocess.run(command, input=data, cwd=cwd, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise SetupError("Command timed out: " + Path(command[0]).name) from None
    if result.returncode:
        raise SetupError("Command failed: " + Path(command[0]).name + " (exit " + str(result.returncode) + "); raw output hidden")
    return result.stdout


def compose_config(bind_ip, port, project, image):
    lan_ip(bind_ip)
    if not upstream.valid_image(image):
        raise SetupError("Use the verified original OmniRoute image digest")
    if not 1024 <= port <= 65535 or not re.fullmatch(r"[a-z][a-z0-9-]{0,49}", project):
        raise SetupError("Invalid unprivileged port or project name")
    hosts = list(dict.fromkeys(["127.0.0.1", bind_ip]))
    return {"name": project, "services": {"omniroute": {
        "image": image, "restart": "unless-stopped", "stop_grace_period": "40s", "env_file": ["gateway.env"],
        "environment": {"DATA_DIR": "/app/data", "PORT": "20128", "REQUIRE_API_KEY": "true", "NEXT_TELEMETRY_DISABLED": "1"},
        "ports": [{"target": 20128, "published": str(port), "host_ip": host, "protocol": "tcp"} for host in hosts],
        "volumes": ["data:/app/data"], "security_opt": ["no-new-privileges:true"],
        "logging": {"driver": "json-file", "options": {"max-size": "10m", "max-file": "3"}},
    }}, "volumes": {"data": {}}}


def key_payload(combo):
    return {"name": "Station inference only", "modelAccessMode": "restricted", "allowedModels": [],
            "allowedCombos": [combo], "scopes": ["inference"], "noLog": True, "allowUsageCommand": False,
            "chaosModeEnabled": False, "usageLimitEnabled": False}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Gateway:
    def __init__(self, base):
        self.base = base.rstrip("/")
        self.opener = urllib.request.build_opener(NoRedirect(), urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def request(self, path, payload=None, method=None, token=None, admin=True, expected=(200, 201)):
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        opener = self.opener if admin else urllib.request.build_opener(NoRedirect())
        request = urllib.request.Request(self.base + path, data=json.dumps(payload).encode() if payload is not None else None,
                                         headers=headers, method=method)
        try:
            with opener.open(request, timeout=75) as response:
                status = response.status
                raw = response.read(2 * 1024 * 1024 + 1)
        except urllib.error.HTTPError as error:
            if error.code in expected:
                return error.code, {}
            raise SetupError("Gateway HTTP " + str(error.code) + " at " + path + "; response hidden") from None
        except (OSError, urllib.error.URLError):
            raise SetupError("Gateway transport failed at " + path) from None
        if status not in expected or len(raw) > 2 * 1024 * 1024:
            raise SetupError("Unexpected gateway response at " + path)
        try:
            return status, json.loads(raw)
        except ValueError:
            raise SetupError("Gateway returned non-JSON at " + path) from None

    def login(self, password):
        self.request("/api/auth/login", {"password": password})


def unwrap(value, field):
    return value[field] if isinstance(value.get(field), dict) else value


def inference_check(api, key, combo=COMBO):
    payload = {"model": combo, "messages": [{"role": "user", "content": "Ответь одним словом: связь"}],
               "stream": False, "max_tokens": 64}
    _, result = api.request("/v1/chat/completions", payload, token=key, admin=False)
    if not str((result.get("choices") or [{}])[0].get("message", {}).get("content") or "").strip():
        raise SetupError("Gateway returned no answer")


def compose(state, *arguments):
    return run(["docker", "compose", "--project-directory", str(state), "-f", str(state / "compose.json"), *arguments], timeout=600)


def owned_stack(state):
    state = Path(state)
    if state.is_symlink() or not state.is_dir() or state.stat().st_uid != os.geteuid() or state.stat().st_mode & 0o077:
        raise SetupError("State must be a private directory owned by this user")
    for path in (state / "stack.json", state / "compose.json"):
        if path.is_symlink() or not path.is_file() or path.stat().st_uid != os.geteuid() or path.stat().st_mode & 0o077:
            raise SetupError("Stack metadata must be private regular files owned by this user")
    metadata = json.loads((state / "stack.json").read_text())
    config = json.loads((state / "compose.json").read_text())
    if (not upstream.valid_image(metadata.get("image")) or
        not re.fullmatch(r"alice-gemini-[a-z0-9-]+", str(metadata.get("project", ""))) or
        config.get("name") != metadata["project"] or
        config.get("services", {}).get("omniroute", {}).get("image") != metadata["image"]):
        raise SetupError("Unknown stack ownership; refusing changes")
    return metadata


def server(args):
    state = args.state_dir.absolute()
    bind_ip = lan_ip(args.bind_ip or input("Server private LAN IPv4 (reachable by Station): ").strip())
    version = getattr(args, "omniroute_version", "latest")
    if args.dry_run:
        print(json.dumps({"mode": "dry-run", "repository": upstream.OMNI_REPO, "release": version,
            "state": str(state), "bind": bind_ip, "port": args.port, "changes": "none"}))
        return
    if state.exists() or state.is_symlink():
        raise FileExistsError("State already exists; use configure/check/rollback, never overwrite")
    run(["docker", "compose", "version"])
    with socket.socket() as probe:
        probe.bind((bind_ip, args.port))
    password = getpass.getpass("New dashboard admin password (at least 16 characters): ")
    if len(password) < 16 or any(c in password for c in "\r\n\0\\"):
        raise SetupError("Use a single-line admin password of at least 16 characters, without backslash")
    if not args.yes and input("Download original OmniRoute and create isolated stack? Type INSTALL: ") != "INSTALL":
        raise SetupError("Not confirmed")
    print("Resolving and verifying original OmniRoute stable release…", flush=True)
    selected = upstream.omniroute(version)
    project = "alice-gemini-" + secrets.token_hex(5)
    config = compose_config(bind_ip, args.port, project, selected["image"])
    create_state(state)
    write_private(state / "compose.json", config)
    write_private(state / "stack.json", {**selected, "project": project, "bind_ip": bind_ip, "port": args.port})
    env = {"JWT_SECRET": secrets.token_hex(48), "API_KEY_SECRET": secrets.token_hex(32),
           "STORAGE_ENCRYPTION_KEY": secrets.token_hex(32), "INITIAL_PASSWORD": password}
    write_private(state / "gateway.env", ("\n".join(k + "='" + v.replace("'", "\\'") + "'" for k, v in env.items()) + "\n").encode())
    try:
        compose(state, "up", "-d", "--wait", "--wait-timeout", "180")
        Gateway("http://127.0.0.1:" + str(args.port)).login(password)
    except BaseException:
        try:
            compose(state, "down")
        except RuntimeError:
            print("Automatic stop failed; run rollback server. Private data retained.", file=sys.stderr)
        raise
    print("Original OmniRoute " + selected["version"] + " is ready; identity saved in stack.json.")
    print("Dashboard: http://" + bind_ip + ":" + str(args.port))
    print("Connect your own Google CLI/OAuth account there, select a model/combo and create an inference-only combo-restricted noLog key.")
    print("Then run: python3 scripts/install.py configure --state-dir " + shlex.quote(str(state)))


def configure(args):
    state = args.state_dir.absolute()
    if state.exists() or state.is_symlink():
        owned_stack(state)  # A bootstrap directory is valid; an arbitrary existing one is not.
        if (state / "station.json").exists() or (state / "station.json").is_symlink():
            raise FileExistsError("Station connection already exists; refusing replacement")
    base_url = validate_endpoint(args.base_url or input("OmniRoute /v1 URL reachable from Station: ").strip())
    model = model_id(args.model or input("Exact combo name in OmniRoute: ").strip())
    key = read_secret(args.api_key_file) if args.api_key_file else getpass.getpass("Restricted OmniRoute key for Station (hidden): ").strip()
    if not key or len(key) > 10000 or any(not 33 <= ord(c) <= 126 for c in key):
        raise SetupError("Station API key is empty or invalid")
    connection = {"llm_base_url": base_url, "llm_model": model, "api_key": key}
    print("Checking OmniRoute inference and permission boundaries…", flush=True)
    check_connection(connection, direct_model="gemini/blocked-direct-probe")
    if not state.exists():
        create_state(state)
    write_private(state / "station.json", connection)
    print("Station connection saved and verified: " + str(state / "station.json"))
    print("Next: python3 scripts/install.py speaker --state-dir " + shlex.quote(str(state)) + " --ssh YOUR_VERIFIED_SSH_ALIAS")


def check_connection(connection, direct_model=None):
    base = validate_endpoint(connection["llm_base_url"])
    api = Gateway(base[:-3])
    key = connection["api_key"]
    api.request("/api/keys", token=key, admin=False, expected=(401, 403))
    api.request("/v1/chat/completions", {"model": connection["llm_model"], "messages": [{"role": "user", "content": "test"}]}, admin=False, expected=(401, 403))
    if direct_model:
        api.request("/v1/chat/completions", {"model": direct_model, "messages": [{"role": "user", "content": "test"}], "stream": False}, token=key, admin=False, expected=(401, 403))
    inference_check(api, key, connection["llm_model"])


def speaker_candidate(raw):
    try:
        config = json.loads(raw)
        if not isinstance(config["aliced"]["uniProxyUrl"], str) or not isinstance(config["aliced"]["uniProxyQuicUrl"], str):
            raise SetupError()
    except (ValueError, KeyError, TypeError):
        raise SetupError("Unsupported firmware config, nothing changed") from None
    config["aliced"]["uniProxyUrl"] = "ws://127.0.0.1:18861/uni.ws"
    config["aliced"]["uniProxyQuicUrl"] = ""
    return (json.dumps(config, ensure_ascii=False, indent=2) + "\n").encode()


def ssh(target, script, data=None, timeout=120):
    target = ssh_target(target)
    return run(["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=10", target, script], data=data, timeout=timeout)


def load_connection(state):
    path = state / "station.json"
    if path.is_symlink() or path.stat().st_mode & 0o077:
        raise SetupError("station.json must be regular and mode 0600")
    connection = json.loads(path.read_text())
    validate_endpoint(connection["llm_base_url"])
    model_id(connection["llm_model"])
    key = connection.get("api_key", "")
    if not isinstance(key, str) or not key or len(key) > 10000 or any(not 33 <= ord(c) <= 126 for c in key):
        raise SetupError("Invalid Station API key; value hidden")
    return connection


def speaker(args):
    target = ssh_target(args.ssh or input("Verified root SSH alias of Station: ").strip())
    state = args.state_dir.absolute()
    if not (state / "station.json").exists():
        raise SetupError("station.json missing; run configure first, no device action executed")
    connection = load_connection(state)
    preflight = (ROOT / "packaging/install-device.sh").read_text()
    if args.dry_run:
        print("Dry-run: read-only speaker preflight; no files, mounts, hooks or reboot.")
        ssh(target, "sh -s -- preflight", preflight.encode())
        return
    if not args.yes and input("Install bridge and arm 10-minute stock recovery? Type INSTALL: ") != "INSTALL":
        raise SetupError("Not confirmed")
    ssh(target, "sh -s -- preflight", preflight.encode())
    original = ssh(target, "cat /system/vendor/quasar/quasar.cfg")
    candidate = speaker_candidate(original)
    print("Downloading original bridge and checking integration patch before device changes…", flush=True)
    with tempfile.TemporaryDirectory(prefix="alice-bridge-build-", dir=os.environ.get("TMPDIR")) as tmp:
        source = Path(tmp) / "quasar-bridge"
        receipt = upstream.prepare_bridge(source, getattr(args, "bridge_version", "latest"))
        receipt = upstream.verify_bridge(source, receipt)
        output = Path(tmp) / "quasar-bridge-arm64"
        upstream.build_bridge(source, output, arch="arm64")
        config = json.loads((ROOT / "config.example.json").read_text())
        config.update(llm_base_url=connection["llm_base_url"], llm_model=connection["llm_model"])
        files = {"bin/quasar-bridge": output.read_bytes(), "config.json": (json.dumps(config, ensure_ascii=False, indent=2) + "\n").encode(),
                 "runtime.env": ("QUASAR_LLM_API_KEY=" + shlex.quote(connection["api_key"]) + "\n").encode(),
                 "original.cfg": original, "candidate.cfg": candidate}
        for name in ("start.sh", "supervise.sh", "rollback-bind.sh", "S98quasarbridge", "recovery.sh", "install-device.sh"):
            files[name] = (ROOT / "packaging" / name).read_bytes()
        files["config.device.example.json"] = files["config.json"]
        for name in ("LICENSE", "NOTICE", "THIRD_PARTY_NOTICES"):
            files[name] = (source / name).read_bytes()
        files["INTEGRATION-NOTICE"] = (ROOT / "NOTICE").read_bytes()
        files["upstream.json"] = (json.dumps(receipt, indent=2) + "\n").encode()
        manifest = {name: hashlib.sha256(data).hexdigest() for name, data in files.items() if name not in ("runtime.env", "original.cfg", "candidate.cfg")}
        files["manifest.json"] = (json.dumps(manifest, indent=2) + "\n").encode()
        # Own firmware backup only; refuse replacing previous installation's evidence.
        backup = state / "speaker-backup"
        create_state(backup)
        write_private(backup / "original.cfg", original)
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w") as tar:
            for name, data in files.items():
                info = tarfile.TarInfo(name)
                info.size, info.mode = len(data), 0o700 if name.endswith(".sh") or name in ("S98quasarbridge", "bin/quasar-bridge") else 0o600
                tar.addfile(info, io.BytesIO(data))
        ssh(target, "umask 077; mkdir " + DEVICE_STATE + " && tar -C " + DEVICE_STATE + " -xf -", archive.getvalue())
        readback = ssh(target, "cd " + DEVICE_STATE + " && sha256sum " + " ".join(manifest))
        observed = {line.split(None, 1)[1].decode().strip().lstrip("*"): line.split(None, 1)[0].decode() for line in readback.splitlines()}
        if observed != manifest:
            raise SetupError("Speaker package hash readback mismatch; no boot changes applied")
    # Device installer validates exact files/backend before any firmware mount/hook.
    ssh(target, "sh " + DEVICE_STATE + "/install-device.sh install", timeout=240)
    print("Installed, pending physical acceptance. The next reboot arms 10-minute recovery.")
    print("Run: ssh " + shlex.quote(target) + " reboot")
    print("Then test voice/stop and run: python3 scripts/install.py confirm --ssh " + shlex.quote(target))


def device_action(args, action):
    target = ssh_target(args.ssh or input("Verified Station SSH alias: ").strip())
    if not args.yes and input("Run " + action + " on Station? Type YES: ") != "YES":
        raise SetupError("Not confirmed")
    ssh(target, "sh " + DEVICE_STATE + "/install-device.sh " + action, timeout=180)
    print("Station " + action + " finished. " + ("Reboot to restore cached stock route." if action == "rollback" else "Physical acceptance acknowledged by operator."))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("all", "server", "speaker", "configure", "check", "confirm", "rollback"))
    parser.add_argument("--state-dir", type=Path, default=Path("/opt/alice-gemini"))
    parser.add_argument("--bind-ip")
    parser.add_argument("--base-url", help="OmniRoute API base URL (e.g. http://127.0.0.1:20128/v1)")
    parser.add_argument("--port", type=int, default=20128)
    parser.add_argument("--model")
    parser.add_argument("--api-key-file", type=Path, help="mode-0600 restricted OmniRoute key file, never a Google token")
    parser.add_argument("--bridge-version", default="latest", help="latest stable or exact original Quasar release tag")
    parser.add_argument("--omniroute-version", default="latest", help="latest stable or exact original OmniRoute release tag")
    parser.add_argument("--ssh", help="already verified SSH alias, never a password")
    parser.add_argument("--target", choices=("server", "speaker"), default="speaker")
    parser.add_argument("--yes", action="store_true", help="explicitly authorize requested action, never skip checks")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.dry_run and args.command not in ("all", "server", "speaker"):
            raise SetupError("--dry-run is supported only for all/server/speaker; no action executed")
        if args.command in ("all", "server"):
            server(args)
            if args.command == "all" and not args.dry_run:
                if input("Finish CLI/OAuth + restricted combo key in dashboard, then type READY (or stop and run configure later): ") != "READY":
                    raise SetupError("Paused after server setup; finish configure before speaker, server retained")
                configure(args)
                speaker(args)
        elif args.command == "speaker":
            speaker(args)
        elif args.command == "configure":
            configure(args)
        elif args.command == "check":
            check_connection(load_connection(args.state_dir))
            print("OmniRoute inference OK; Station key has no management access.")
        elif args.command == "rollback" and args.target == "server":
            state = args.state_dir.absolute()
            owned_stack(state)
            if not args.yes and input("Stop only this OmniRoute stack, keeping its data? Type YES: ") != "YES":
                raise SetupError("Not confirmed")
            compose(state, "down")
            print("Own stack stopped; private data and volume retained.")
        else:
            if args.command == "confirm" and args.yes:
                raise SetupError("Voice acceptance requires interactive confirmation, not --yes")
            device_action(args, args.command)
        return 0
    except (ValueError, OSError, RuntimeError, KeyError, subprocess.TimeoutExpired) as error:
        print("STOP: " + (str(error) if isinstance(error, (SetupError, upstream.UpstreamError)) else type(error).__name__) + ". No secrets printed; inspect local state before retrying.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
