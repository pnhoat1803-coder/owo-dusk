"""Where the dashboard listens, and which addresses other devices on the network can open it on.

- website.host is the IPv4 address to listen on. The default is 0.0.0.0, so the whole home network can connect. A
  missing or empty website.host means 0.0.0.0. website.enableHost is not read any more: an old file that has it false
  listens on the LAN too, and only website.host = 127.0.0.1 keeps the dashboard on this machine.
- A LAN address needs a password of at least MIN_PASSWORD characters. Without one (or with a shorter one), the
  dashboard listens on 127.0.0.1 instead (effective_bind), prints one warning and shows a banner. It never serves the
  LAN without a password.
- LAN addresses are read from the network setup without sending a packet: a UDP connect only picks the outgoing
  interface, and getaddrinfo asks for the address of this machine's own host name.
"""

import ipaddress
import socket

DEFAULT_HOST = "0.0.0.0"
LOCAL_HOST = "127.0.0.1"
MIN_PASSWORD = 8
PROBE_ADDRESS = ("10.255.255.255", 1)
# The console line for each reason the dashboard listens on this machine only. {min} is MIN_PASSWORD, {host} the address
WARNINGS = {
    "no_password": (
        "Dashboard chỉ mở trên máy này (127.0.0.1) vì chưa đặt mật khẩu dashboard. "
        "Đặt website.password dài ít nhất {min} ký tự để mở cho mạng LAN."
    ),
    "short_password": (
        "Dashboard chỉ mở trên máy này (127.0.0.1) vì mật khẩu dashboard ngắn hơn {min} ký tự. "
        "Đặt website.password dài hơn để mở cho mạng LAN."
    ),
    "invalid_host": (
        "Dashboard chỉ mở trên máy này (127.0.0.1) vì website.host không phải địa chỉ IPv4. "
        "Đặt website.host = 0.0.0.0 để mở cho mạng LAN."
    ),
    "local_only_config": (
        "Dashboard chỉ mở trên máy này (127.0.0.1) vì website.host = {host}. "
        "Đặt website.host = 0.0.0.0 để mở cho mạng LAN."
    ),
}


def is_ipv4(value):
    if not isinstance(value, str):
        return False
    try:
        ipaddress.IPv4Address(value.strip())
    except ValueError:
        return False
    return True


def is_loopback(host):
    if not isinstance(host, str):
        return False
    text = host.strip().lower()
    if text == "localhost":
        return True
    try:
        return ipaddress.ip_address(text).is_loopback
    except ValueError:
        return False


def configured_host(website):
    """The address the dashboard listens on, from the website block of global_settings.json (a dict): website.host as
    written. A missing or empty website.host gives DEFAULT_HOST. website.enableHost is not read: effective_bind decides
    what is really used"""
    if not isinstance(website, dict):
        return DEFAULT_HOST
    host = website.get("host")
    if not isinstance(host, str) or not host.strip():
        return DEFAULT_HOST
    return host.strip()


def effective_bind(configured, password):
    """(address to listen on, reason). The one decision every launch path uses. A missing or empty address means
    DEFAULT_HOST. The reason is one of:
    lan_ok             a LAN address and a password of MIN_PASSWORD characters or more
    local_only_config  an address of this machine (127.*, localhost, ::1): the owner chose it
    no_password        a LAN address without a password: listens on 127.0.0.1
    short_password     a LAN address and a password shorter than MIN_PASSWORD: listens on 127.0.0.1
    invalid_host       not an IPv4 address: listens on 127.0.0.1"""
    host = configured.strip() if isinstance(configured, str) else ""
    if not host:
        host = DEFAULT_HOST
    if is_loopback(host):
        return host, "local_only_config"
    if not is_ipv4(host):
        return LOCAL_HOST, "invalid_host"
    secret = password if isinstance(password, str) else ""
    if not secret:
        return LOCAL_HOST, "no_password"
    if len(secret) < MIN_PASSWORD:
        return LOCAL_HOST, "short_password"
    return host, "lan_ok"


def shows_password_banner(reason):
    """True when the dashboard page shows the password banner: the two reasons that a longer password would fix"""
    return reason in ("no_password", "short_password")


def usable_lan_ipv4(address):
    """True for an IPv4 address other devices can use. Loopback, link-local (169.254.*), 0.0.0.0, multicast and reserved
    addresses are not"""
    if not is_ipv4(address):
        return False
    ip = ipaddress.IPv4Address(address.strip())
    return not (ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast or ip.is_reserved)


def lan_addresses():
    """The IPv4 addresses of this machine that other devices can reach: private ranges (10.*, 172.16-31.*, 192.168.*)
    first. Sends no packet. Returns [] when the network is not available"""
    found = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(PROBE_ADDRESS)  # UDP: this only chooses the outgoing interface, nothing is sent
            found.append(probe.getsockname()[0])
    except Exception:  # no network: there are no addresses to show
        pass
    try:
        for entry in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.append(entry[4][0])
    except Exception:
        pass
    unique = []
    for address in found:
        if usable_lan_ipv4(address) and address not in unique:
            unique.append(address)
    private = [a for a in unique if ipaddress.IPv4Address(a).is_private]
    others = [a for a in unique if a not in private]
    return private + others


def lan_urls(bind_host, port, addresses=None):
    """The URLs other devices use: an empty list when the dashboard listens on this machine only. One URL for a single
    bind address, and every LAN address for 0.0.0.0"""
    if is_loopback(bind_host):
        return []
    if bind_host == DEFAULT_HOST:
        ips = lan_addresses() if addresses is None else list(addresses)
    else:
        ips = [bind_host]
    return [f"http://{ip}:{int(port)}" for ip in ips]


def startup_lines(bind_host, port, urls, reason):
    """The block the dashboard prints when it starts, and the bot prints too. Listening on a LAN address: one line per
    LAN address, then this machine's address. Listening on this machine only: the warning for the reason, then this
    machine's address"""
    local = f"Dashboard (máy này): http://{LOCAL_HOST}:{int(port)}"
    if is_loopback(bind_host):
        lines = []
        if reason in WARNINGS:
            lines.append(WARNINGS[reason].format(min=MIN_PASSWORD, host=bind_host))
        return lines + [local]
    lines = [f"Dashboard (LAN): {url}" for url in urls]
    if not lines:
        lines.append("Dashboard (LAN): không tìm thấy địa chỉ LAN của máy này, kiểm tra kết nối mạng")
    return lines + [local]


def dashboard_listening(port, timeout=0.5):
    """True when something accepts connections on this machine's dashboard port"""
    try:
        with socket.create_connection((LOCAL_HOST, int(port)), timeout=timeout):
            return True
    except Exception:
        return False
