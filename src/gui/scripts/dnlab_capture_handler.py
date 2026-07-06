#!/usr/bin/env python3
"""Local protocol handler for DNLab Wireshark captures."""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path


SCHEME = "dnlab-capture"
CHUNK_SIZE = 1024 * 128


def main() -> int:
    parser = argparse.ArgumentParser(description="DNLab capture browser handler")
    sub = parser.add_subparsers(dest="cmd", required=True)
    open_p = sub.add_parser("open", help="Open a dnlab-capture:// URL")
    open_p.add_argument("url")
    sub.add_parser("install", help="Register dnlab-capture:// on this workstation")
    sub.add_parser("uninstall", help="Remove the local dnlab-capture:// registration")
    sub.add_parser("doctor", help="Check handler prerequisites")
    args = parser.parse_args()

    if args.cmd == "open":
        return open_capture(args.url)
    if args.cmd == "install":
        return install()
    if args.cmd == "uninstall":
        return uninstall()
    if args.cmd == "doctor":
        return doctor()
    return 2


def open_capture(raw_url: str) -> int:
    params = _parse_handler_url(raw_url)
    status_url = params.get("status_url", "")
    stream_url = params.get("stream_url", "")
    title = params.get("title", "DNLab capture")
    if not status_url or not stream_url:
        return _fail("Missing status_url or stream_url in handler URL")

    wireshark = _find_wireshark()
    if not wireshark:
        return _fail("Wireshark was not found. Install Wireshark or add it to PATH.")

    try:
        status = _fetch_json(status_url)
    except Exception as exc:
        return _fail(f"DNLab capture preflight failed: {exc}")
    if not status.get("ok"):
        code = status.get("code") or "capture_error"
        detail = status.get("detail") or "Capture is not available"
        return _fail(f"DNLab capture preflight failed [{code}]: {detail}")

    print(f"Opening {title}", flush=True)
    proc = subprocess.Popen([wireshark, "-k", "-i", "-"], stdin=subprocess.PIPE)
    assert proc.stdin is not None
    result = 0
    try:
        with urllib.request.urlopen(stream_url, timeout=1) as stream:
            while True:
                if proc.poll() is not None:
                    break
                try:
                    chunk = stream.read(CHUNK_SIZE)
                except (TimeoutError, socket.timeout):
                    continue
                if not chunk:
                    break
                proc.stdin.write(chunk)
                proc.stdin.flush()
    except BrokenPipeError:
        result = 0
    except Exception as exc:
        result = _fail(f"DNLab capture stream failed: {exc}")
    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass
    if result:
        try:
            proc.terminate()
        except Exception:
            pass
    return result or proc.wait()


def install() -> int:
    system = platform.system().lower()
    if system == "windows":
        return _install_windows()
    if system == "linux":
        return _install_linux()
    if system == "darwin":
        return _install_macos()
    return _fail(f"Unsupported platform: {platform.system()}")


def uninstall() -> int:
    system = platform.system().lower()
    if system == "windows":
        return _uninstall_windows()
    if system == "linux":
        return _uninstall_linux()
    if system == "darwin":
        return _uninstall_macos()
    return _fail(f"Unsupported platform: {platform.system()}")


def doctor() -> int:
    print(f"Platform: {platform.system()} {platform.release()}")
    print(f"Python: {sys.executable}")
    print(f"Handler script: {_script_path()}")
    wireshark = _find_wireshark()
    print(f"Wireshark: {wireshark or 'not found'}")
    return 0 if wireshark else 1


def _parse_handler_url(raw_url: str) -> dict[str, str]:
    parsed = urllib.parse.urlparse(raw_url)
    if parsed.scheme != SCHEME:
        raise ValueError(f"Unsupported URL scheme: {parsed.scheme}")
    if parsed.netloc != "open":
        raise ValueError(f"Unsupported DNLab capture action: {parsed.netloc}")
    values = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    return {key: vals[-1] for key, vals in values.items() if vals}


def _fetch_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as res:
        data = res.read(1024 * 1024)
    return json.loads(data.decode("utf-8"))


def _find_wireshark() -> str | None:
    found = shutil.which("wireshark") or shutil.which("Wireshark.exe")
    if found:
        return found
    candidates: list[Path] = []
    if platform.system().lower() == "windows":
        for env in ("ProgramFiles", "ProgramFiles(x86)"):
            base = os.environ.get(env)
            if base:
                candidates.append(Path(base) / "Wireshark" / "Wireshark.exe")
    elif platform.system().lower() == "darwin":
        candidates.append(Path("/Applications/Wireshark.app/Contents/MacOS/Wireshark"))
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return None


def _install_windows() -> int:
    import winreg

    command = f'"{sys.executable}" "{_script_path()}" open "%1"'
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, rf"Software\Classes\{SCHEME}") as key:
        winreg.SetValueEx(key, None, 0, winreg.REG_SZ, "URL:DNLab Capture")
        winreg.SetValueEx(key, "URL Protocol", 0, winreg.REG_SZ, "")
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, rf"Software\Classes\{SCHEME}\shell\open\command") as key:
        winreg.SetValueEx(key, None, 0, winreg.REG_SZ, command)
    print(f"Registered {SCHEME}:// for current user")
    return 0


def _uninstall_windows() -> int:
    import winreg

    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, rf"Software\Classes\{SCHEME}\shell\open\command")
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, rf"Software\Classes\{SCHEME}\shell\open")
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, rf"Software\Classes\{SCHEME}\shell")
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, rf"Software\Classes\{SCHEME}")
    except FileNotFoundError:
        pass
    print(f"Unregistered {SCHEME}:// for current user")
    return 0


def _install_linux() -> int:
    app_dir = Path.home() / ".local/share/applications"
    app_dir.mkdir(parents=True, exist_ok=True)
    desktop = app_dir / "dnlab-capture-handler.desktop"
    desktop.write_text(
        "[Desktop Entry]\n"
        "Name=DNLab Capture Handler\n"
        "Type=Application\n"
        f"Exec=\"{sys.executable}\" \"{_script_path()}\" open %u\n"
        "Terminal=true\n"
        "NoDisplay=true\n"
        f"MimeType=x-scheme-handler/{SCHEME};\n",
        encoding="utf-8",
    )
    subprocess.run(["xdg-mime", "default", desktop.name, f"x-scheme-handler/{SCHEME}"], check=False)
    subprocess.run(["update-desktop-database", str(app_dir)], check=False)
    print(f"Registered {SCHEME}:// via {desktop}")
    return 0


def _uninstall_linux() -> int:
    desktop = Path.home() / ".local/share/applications/dnlab-capture-handler.desktop"
    if desktop.exists():
        desktop.unlink()
    print(f"Removed {desktop}")
    return 0


def _install_macos() -> int:
    app_dir = Path.home() / "Applications/DNLab Capture Handler.app"
    app_dir.parent.mkdir(parents=True, exist_ok=True)
    command_prefix = _shell_quote(sys.executable) + " " + _shell_quote(_script_path()) + " open "
    apple_script = (
        "on open location this_url\n"
        f"  do shell script {_applescript_string(command_prefix)} & quoted form of this_url\n"
        "end open location\n"
    )
    if shutil.which("osacompile"):
        subprocess.run(["osacompile", "-o", str(app_dir), "-e", apple_script], check=True)
        plist = app_dir / "Contents/Info.plist"
        text = plist.read_text(encoding="utf-8")
        if "CFBundleURLTypes" not in text:
            text = text.replace(
                "</dict>\n</plist>",
                f"  <key>CFBundleURLTypes</key>\n  <array><dict>\n"
                f"    <key>CFBundleURLName</key><string>DNLab Capture</string>\n"
                f"    <key>CFBundleURLSchemes</key><array><string>{SCHEME}</string></array>\n"
                f"  </dict></array>\n</dict>\n</plist>",
            )
            plist.write_text(text, encoding="utf-8")
    else:
        return _fail("osacompile not found; cannot register macOS URL handler")
    subprocess.run(["/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister", "-f", str(app_dir)], check=False)
    print(f"Registered {SCHEME}:// via {app_dir}")
    return 0


def _uninstall_macos() -> int:
    app_dir = Path.home() / "Applications/DNLab Capture Handler.app"
    if app_dir.exists():
        shutil.rmtree(app_dir)
    print(f"Removed {app_dir}")
    return 0


def _script_path() -> str:
    return str(Path(__file__).resolve())


def _shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _applescript_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _fail(message: str) -> int:
    print(message, file=sys.stderr)
    if platform.system().lower() == "windows":
        try:
            input("Press Enter to close...")
        except Exception:
            pass
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
