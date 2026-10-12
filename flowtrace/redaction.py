import hashlib
import logging
import math
import os
import re
from collections import Counter

REDACTED = "<redacted>"

# Strict environment-level override: if FLOWTRACE_STRICT_PRIVACY is set,
# redaction CANNOT be disabled under any circumstances (anti-tamper / anti-prompt injection).
STRICT_PRIVACY_ENV = os.environ.get("FLOWTRACE_STRICT_PRIVACY", "1") == "1"

DEFAULT_SENSITIVE_KEY_RE = re.compile(
    r"(password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|"
    r"auth(?:orization)?|credential|private[_-]?key|client[_-]?secret|"
    r"session[_-]?id|ssn|social[_-]?security|credit[_-]?card|cvv|pin|"
    r"bearer|refresh[_-]?token|db[_-]?pass|database_url|connection[_-]?string)",
    re.IGNORECASE,
)

_VALUE_PATTERNS = (
    ("pem_private_key", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----", re.IGNORECASE)),
    ("db_connection_uri", re.compile(r"(?:postgres|postgresql|mysql|mongodb(?:\+srv)?|redis|mssql|oracle)://[^\s:]+:[^\s@]+@[^\s/]+", re.IGNORECASE)),
    ("jwt", re.compile(r"^eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+$")),
    ("aws_access_key", re.compile(r"^(?:AKIA|ABIA|ACCA|ASIA)[0-9A-Z]{16}$")),
    ("aws_secret_key", re.compile(r"^[A-Za-z0-9/+=]{40}$")),
    ("github_token", re.compile(r"^(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{80,})$")),
    ("slack_token", re.compile(r"^xox[baprs]-[0-9]{10,13}-[0-9]{10,13}-[a-zA-Z0-9]{24,}$")),
    ("stripe_key", re.compile(r"^[rs]k_(?:live|test)_[0-9a-zA-Z]{24,}$")),
    ("openai_key", re.compile(r"^sk-(?:proj-)?[A-Za-z0-9_-]{40,}$")),
    ("google_api_key", re.compile(r"^AIza[0-9A-Za-z-_]{35}$")),
    ("credit_card", re.compile(r"^(?:4[0-9]{12}(?:[0-9]{3})?|5[1-5][0-9]{14}|3[47][0-9]{13}|3(?:0[0-5]|[68][0-9])[0-9]{11}|6(?:011|5[0-9]{2})[0-9]{12})$")),
    ("us_ssn", re.compile(r"^(?!000|666|9\d{2})\d{3}-(?!00)\d{2}-(?!0000)\d{4}$")),
    ("bearer_token", re.compile(r"^Bearer\s+[A-Za-z0-9\-_.~+/]+=*$", re.IGNORECASE)),
)

_HIGH_ENTROPY_CHARSET = re.compile(r"^[A-Za-z0-9+/_=.-]+$")
_HIGH_ENTROPY_MIN_LEN = 24
_HIGH_ENTROPY_MIN_BITS = 4.0

_SALT = os.environ.get("FLOWTRACE_SALT", "flowtrace-deterministic-salt-2026").encode("utf-8")


def _shannon_entropy(s: str) -> float:
    if not s:
        return 0.0
    counts = Counter(s)
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def _hash_token(val: str) -> str:
    """Produces a non-reversible cryptographic fingerprint of a redacted value.
    
    This allows AI and dataflow algorithms to confirm that two redacted values match
    (e.g. auth.return ==> api.call) without exposing the secret itself.
    """
    digest = hashlib.sha256(_SALT + str(val).encode("utf-8", errors="ignore")).hexdigest()
    return digest[:12]


class Redactor:
    def __init__(self, config=None, audit_log_path="redaction_audit.log"):
        self.config = config or {}
        self.audit_log_path = audit_log_path
        
        # If STRICT_PRIVACY_ENV is set, enabled cannot be toggled to False
        if STRICT_PRIVACY_ENV:
            self.enabled = True
        else:
            self.enabled = self.config.get("enabled", True)
            
        self.audit_enabled = self.config.get("audit_enabled", True)
        self.preserve_hash = self.config.get("preserve_hash", True)

        custom_key_pattern = self.config.get("sensitive_key_pattern")
        if custom_key_pattern:
            self.sensitive_key_re = re.compile(custom_key_pattern, re.IGNORECASE)
        else:
            self.sensitive_key_re = DEFAULT_SENSITIVE_KEY_RE

        self.logger = None
        self._setup_audit_logger()

    def _setup_audit_logger(self):
        if not self.audit_enabled:
            return
        self.logger = logging.getLogger("flowtrace.redactor")
        self.logger.setLevel(logging.INFO)
        if not self.logger.handlers:
            try:
                fh = logging.FileHandler(self.audit_log_path, encoding="utf-8")
                fh.setFormatter(logging.Formatter("%(asctime)s - [AUDIT] - %(message)s"))
                self.logger.addHandler(fh)
            except Exception:
                pass

    def _log_audit(self, reason, key=None):
        if self.audit_enabled and self.logger:
            key_info = f" under key '{key}'" if key else ""
            self.logger.info(f"Redacted value matching '{reason}' pattern{key_info}.")

    def is_sensitive_key(self, key):
        return bool(key) and bool(self.sensitive_key_re.search(str(key)))

    def looks_like_secret_value(self, value):
        if not isinstance(value, str) or len(value) < 10:
            return None
            
        # Strip string whitespace
        val = value.strip()
        
        for label, pattern in _VALUE_PATTERNS:
            if pattern.search(val):
                return label
                
        if (len(val) >= _HIGH_ENTROPY_MIN_LEN
                and _HIGH_ENTROPY_CHARSET.match(val)
                and _shannon_entropy(val) >= _HIGH_ENTROPY_MIN_BITS):
            return "high_entropy_token"
            
        return None

    def redact(self, value, key=None, _seen=None):
        if not self.enabled:
            return value

        # 1. Key-name check (e.g. password, auth, session_token, secret)
        if self.is_sensitive_key(key):
            self._log_audit(f"key={key}", key=key)
            hash_suffix = f":sha256={_hash_token(value)}" if self.preserve_hash and value is not None else ""
            return f"{REDACTED}:key={key}{hash_suffix}"

        # 2. Value pattern & entropy check (e.g. AWS keys, DB URIs, JWTs, Stripe, OpenAI)
        label = self.looks_like_secret_value(value)
        if label:
            self._log_audit(label, key=key)
            hash_suffix = f":sha256={_hash_token(value)}" if self.preserve_hash else ""
            return f"{REDACTED}:{label}{hash_suffix}"

        # 3. Handle dictionary embedded values recursively if passed
        if isinstance(value, dict):
            if _seen is None:
                _seen = frozenset()
            if id(value) in _seen:
                return value
            _seen = _seen | {id(value)}
            return {k: self.redact(v, key=k, _seen=_seen) for k, v in value.items()}

        if isinstance(value, (list, tuple, set)):
            if _seen is None:
                _seen = frozenset()
            if id(value) in _seen:
                return value
            _seen = _seen | {id(value)}
            return [self.redact(v, _seen=_seen) for v in value]

        return value


# Fallback global redactor for backward compatibility
_global_redactor = Redactor()

def is_sensitive_key(key):
    return _global_redactor.is_sensitive_key(key)

def looks_like_secret_value(value):
    return _global_redactor.looks_like_secret_value(value)

def redact(value, key=None, _seen=None):
    return _global_redactor.redact(value, key, _seen=_seen)


