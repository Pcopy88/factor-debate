import ipaddress
import re
import socket
from urllib.parse import urlsplit
import keyring

SERVICE = "FundamentalLab"


def get_secret(name):
    try:
        return keyring.get_password(SERVICE, name)
    except Exception:
        return None


def set_secret(name, value):
    if not re.fullmatch(r"[a-zA-Z0-9_.-]{1,80}", name):
        raise ValueError("凭据名称只能使用字母、数字、点、下划线和短横线")
    backend = keyring.get_keyring()
    if "Windows" not in type(backend).__name__ and "WinVault" not in type(backend).__name__:
        raise RuntimeError("请配置系统凭据后端，禁止将密钥存入明文文件")
    keyring.set_password(SERVICE, name, value)


def public_url(url):
    p = urlsplit(url)
    if p.scheme != "https" or not p.hostname or p.username or p.password or p.port not in (None, 443):
        raise ValueError("只允许没有用户名、密码或自定义端口的 HTTPS 公网地址")
    try:
        infos = socket.getaddrinfo(p.hostname, 443, type=socket.SOCK_STREAM)
    except OSError as e:
        raise ValueError("无法解析来源地址") from e
    if not infos or any(not ipaddress.ip_address(x[4][0]).is_global for x in infos):
        raise ValueError("不允许访问本机、内网或保留地址")
    return url


def redact(text, secrets=()):
    text = str(text)
    for secret in secrets:
        if secret: text = text.replace(secret, "[已隐藏凭据]")
    return re.sub(r"\b(?:sk-|tvly-)[A-Za-z0-9_-]{12,}", "[已隐藏凭据]", text)

