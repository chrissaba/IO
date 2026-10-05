"""Web Push: notifications on a paired phone (a run finished, IO has a question, an approval waits).

The phone's browser gives IO a subscription (an endpoint at Apple's, Google's or Mozilla's push service, and two keys).
IO encrypts each message for that phone alone (RFC 8291, aes128gcm) and signs the request with its own VAPID key
(RFC 8292), so the push service carries it without being able to read it. The VAPID key is made once and kept in
data/vapid.json. Uses `cryptography` and `jwt`, which IO's other packages already bring."""
import base64
import json
import os
import struct
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import jwt
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

KEY_FILE = Path(__file__).parent / "data" / "vapid.json"
# who to contact about these pushes, which push services ask for (no personal address)
CONTACT = "https://github.com/chrissaba/IO"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _raw_public(key: ec.EllipticCurvePrivateKey) -> bytes:
    return key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


def _vapid() -> ec.EllipticCurvePrivateKey:
    try:
        pem = json.loads(KEY_FILE.read_text(encoding="utf-8"))["private_pem"]
        return serialization.load_pem_private_key(pem.encode(), password=None)
    except (OSError, ValueError, KeyError):
        key = ec.generate_private_key(ec.SECP256R1())
        pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
        KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
        KEY_FILE.write_text(json.dumps({"private_pem": pem}), encoding="utf-8")
        return key


def public_key() -> str:
    """The applicationServerKey a phone subscribes with."""
    return _b64(_raw_public(_vapid()))


def _hkdf(salt: bytes, ikm: bytes, info: bytes, length: int) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=salt, info=info).derive(ikm)


def encrypt(payload: bytes, p256dh: str, auth: str, ephemeral=None, salt: bytes = b"") -> bytes:
    """One aes128gcm record for this subscription (RFC 8291). ephemeral and salt are fresh each time; they're
    parameters only so the RFC's worked example can check this."""
    ua_public = _unb64(p256dh)
    secret = _unb64(auth)
    ephemeral = ephemeral or ec.generate_private_key(ec.SECP256R1())
    as_public = _raw_public(ephemeral)
    shared = ephemeral.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public))
    ikm = _hkdf(secret, shared, b"WebPush: info\x00" + ua_public + as_public, 32)
    salt = salt or os.urandom(16)
    cek = _hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
    body = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)  # \x02: the last (and only) record
    return salt + struct.pack(">IB", 4096, len(as_public)) + as_public + body


def send(subscription: dict, message: dict, ttl: int = 86400) -> int:
    """Sends one notification; the push service's status (201 = accepted, 404/410 = this subscription is gone), 0 when
    it couldn't be reached."""
    endpoint = subscription["endpoint"]
    keys = subscription.get("keys") or {}
    url = urllib.parse.urlsplit(endpoint)
    key = _vapid()
    token = jwt.encode({"aud": f"{url.scheme}://{url.netloc}", "exp": int(time.time()) + 12 * 3600, "sub": CONTACT}, key, algorithm="ES256")
    req = urllib.request.Request(endpoint, data=encrypt(json.dumps(message).encode(), keys["p256dh"], keys["auth"]), method="POST", headers={
        "Content-Encoding": "aes128gcm", "Content-Type": "application/octet-stream", "TTL": str(ttl), "Urgency": "high",
        "Authorization": f"vapid t={token}, k={_b64(_raw_public(key))}",
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except OSError:
        return 0
