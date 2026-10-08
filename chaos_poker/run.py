"""Start Chaos Poker on the laptop: LAN IP, HTTPS certificate, uvicorn on 0.0.0.0:8443 (section 2).

    python run.py              # window with the host QR code (if tkinter is available)
    python run.py --no-gui     # console only: the QR code is printed in the terminal
"""
from __future__ import annotations

import argparse
import os
import shutil
import socket
import ssl
import subprocess
import sys
import threading
from pathlib import Path

import qrcode
import uvicorn
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
CERT_DIR = BASE_DIR / "certs"
PORT = 8443
# Marks a certificate created by this script (it is regenerated when the LAN IP changes).
GENERATED_MARK = ".generated_for"
# OpenSSL shipped with Git for Windows, in case it is not on PATH.
WINDOWS_OPENSSL = (
    Path(r"C:\Program Files\Git\usr\bin\openssl.exe"),
    Path(r"C:\Program Files\Git\mingw64\bin\openssl.exe"),
)


def load_environment(env_path: Path | None = None) -> None:
    path = env_path or BASE_DIR.parent / ".env"
    if path.exists():
        load_dotenv(path, override=False)


def find_openssl() -> str:
    found = shutil.which("openssl")
    if found:
        return found
    for candidate in WINDOWS_OPENSSL:
        if candidate.exists():
            return str(candidate)
    return "openssl"


def has_subject_alt_name(cert_file: Path) -> bool:
    try:
        decoded = ssl._ssl._test_decode_cert(str(cert_file))  # stdlib decoder, no extra packages
    except Exception:
        return True  # cannot tell: leave the file alone
    return bool(decoded.get("subjectAltName"))


def ensure_local_certificates(cert_dir: Path, host_ip: str | None = None) -> None:
    """Use certs/cert.pem + key.pem (e.g. from mkcert); otherwise create a self-signed one for the LAN IP."""
    cert_dir.mkdir(parents=True, exist_ok=True)
    cert_file = cert_dir / "cert.pem"
    key_file = cert_dir / "key.pem"
    mark = cert_dir / GENERATED_MARK
    if cert_file.exists() and key_file.exists():
        generated_for = mark.read_text(encoding="utf-8").strip() if mark.exists() else None
        if generated_for is None and not has_subject_alt_name(cert_file):
            # An old certificate without subjectAltName is rejected for https://<IP>: keep a copy, make a new one.
            for file in (cert_file, key_file):
                file.replace(file.with_name(file.name + ".old"))
        # A user certificate (mkcert) is never touched; ours is renewed when the IP changes.
        elif generated_for is None or host_ip is None or generated_for == host_ip:
            return
    names = ["DNS:localhost", "IP:127.0.0.1"] + ([f"IP:{host_ip}"] if host_ip else [])
    command = [
        find_openssl(), "req", "-x509", "-nodes", "-newkey", "rsa:2048",
        "-keyout", str(key_file), "-out", str(cert_file), "-days", "365",
        "-subj", "/CN=Chaos Poker",
        "-addext", "subjectAltName=" + ",".join(names),
    ]
    try:
        subprocess.run(command, check=True, capture_output=True)
    except (OSError, subprocess.CalledProcessError) as error:
        raise SystemExit(
            "Не удалось создать HTTPS-сертификат (нужен openssl). Варианты:\n"
            "  1) установите mkcert и выполните в папке chaos_poker:\n"
            f"     mkcert -cert-file certs/cert.pem -key-file certs/key.pem {host_ip or '<IP>'} localhost 127.0.0.1\n"
            "  2) установите Git для Windows (в нём есть openssl) и запустите снова.\n"
            f"Подробности: {type(error).__name__}"
        ) from error
    if host_ip:
        mark.write_text(host_ip, encoding="utf-8")


def _route_ip() -> str | None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))   # no packet is sent: this only picks the outgoing interface
        return sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()


def _ip_rank(ip: str) -> int:
    """Lower is better: home Wi-Fi ranges first; VPN (100.64/10), link-local and loopback last."""
    parts = [int(part) for part in ip.split(".")]
    if parts[0] == 192 and parts[1] == 168:
        return 1 if parts[2] == 137 else 0      # 192.168.137.x is the Windows hotspot
    if parts[0] == 10:
        return 2
    if parts[0] == 172 and 16 <= parts[1] <= 31:
        return 2
    if parts[0] == 100 and 64 <= parts[1] <= 127:
        return 5                                 # carrier-grade NAT: usually a VPN
    if parts[0] == 169 and parts[1] == 254 or parts[0] == 127:
        return 9
    return 4


def candidate_ips() -> list[str]:
    found = []
    route = _route_ip()
    if route:
        found.append(route)
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.append(info[4][0])
    except OSError:
        pass
    unique = list(dict.fromkeys(found))
    # Stable sort: among equally good addresses the routing choice stays first.
    return sorted(unique, key=_ip_rank)


def detect_local_ip() -> str:
    """LAN IP for the QR codes. HOST_IP in .env wins (e.g. when a VPN confuses the detection)."""
    configured = os.getenv("HOST_IP")
    if configured:
        return configured
    candidates = candidate_ips()
    return candidates[0] if candidates and _ip_rank(candidates[0]) < 9 else "127.0.0.1"


def run_server(cert_file: Path, key_file: Path, port: int = PORT) -> None:
    uvicorn.run("app.main:app", host="0.0.0.0", port=port,
                ssl_keyfile=str(key_file), ssl_certfile=str(cert_file), reload=False)


def print_console_qr(url: str) -> None:
    qr = qrcode.QRCode(border=1)
    qr.add_data(url)
    try:
        qr.print_ascii(invert=True)
    except UnicodeEncodeError:
        print("(QR-код не помещается в эту консоль — откройте ссылку ниже на главном телефоне)")


def show_window(url: str) -> bool:
    """A small window with the host QR code. Returns False when there is no GUI."""
    try:
        import tkinter as tk
        from PIL import ImageTk
    except ImportError:
        return False
    try:
        root = tk.Tk()
    except tk.TclError:
        return False
    root.title("Chaos Poker — QR-код ведущего")
    root.geometry("420x540")
    root.resizable(False, False)
    tk.Label(root, text="Отсканируйте главным телефоном (host)", font=("Arial", 14, "bold")).pack(pady=15)
    image = ImageTk.PhotoImage(qrcode.make(url).resize((300, 300)))
    label = tk.Label(root, image=image)
    label.image = image  # keep a reference, otherwise the image is garbage collected
    label.pack(pady=5)
    tk.Label(root, text=url, font=("Arial", 9), fg="blue", wraplength=390).pack(pady=15)
    tk.Label(root, text="Закройте это окно, чтобы остановить сервер.", font=("Arial", 9), fg="gray").pack(side=tk.BOTTOM, pady=10)
    root.mainloop()
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Chaos Poker server")
    parser.add_argument("--no-gui", action="store_true", help="do not open the window with the QR code")
    parser.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args()

    load_environment()
    host_ip = detect_local_ip()
    os.environ.setdefault("HOST_IP", host_ip)
    ensure_local_certificates(CERT_DIR, host_ip)
    cert_file, key_file = CERT_DIR / "cert.pem", CERT_DIR / "key.pem"

    os.chdir(BASE_DIR)
    from app.main import rooms  # imported after .env is loaded

    # A room is created right away: the host scans this QR code instead of creating one.
    room = rooms.create_room(host_name="Ведущий")
    os.environ.setdefault("PORT", str(args.port))
    from app.main import public_base_url

    # With a tunnel (PUBLIC_URL) the phones use its address instead of the LAN IP.
    host_url = f"{public_base_url()}/host?token={room.host_token}"

    print("\n=== Chaos Poker ===")
    print(f"Адрес сервера: https://{host_ip}:{args.port}" + (f"  (туннель: {os.environ['PUBLIC_URL']})" if os.getenv("PUBLIC_URL") else ""))
    others = [ip for ip in candidate_ips() if ip != host_ip and _ip_rank(ip) < 9]
    if others:
        print(f"Другие адреса ноутбука: {', '.join(others)} — если телефоны не открывают ссылку, "
              "укажите нужный в .env: HOST_IP=...")
    print("Главный телефон (host) — отсканируйте QR-код или откройте ссылку:")
    print_console_qr(host_url)
    print(host_url)
    print("Телефоны должны быть в той же Wi-Fi сети. Если не открывается — см. README, раздел «Файрвол».")
    print("Остановить сервер: Ctrl+C" + ("" if args.no_gui else " или закрыть окно с QR-кодом") + "\n")
    sys.stdout.flush()

    if args.no_gui:
        run_server(cert_file, key_file, args.port)
        return
    threading.Thread(target=run_server, args=(cert_file, key_file, args.port), daemon=True).start()
    if not show_window(host_url):
        print("Окно недоступно, сервер работает в консоли.")
        threading.Event().wait()


if __name__ == "__main__":
    main()
