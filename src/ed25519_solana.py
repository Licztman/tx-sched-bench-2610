#!/usr/bin/env python3
"""
Pure-Python RFC 8032 Ed25519 signing, Base58 codec, Solana wire-format transaction
builder (Legacy + ComputeBudget and Post-Alpenglow v1 parser), and atomic SpendCapGuard.

Zero external C/Rust dependencies required; works identically in local Python 3
and inside Docker containers.
"""

import base64
import hashlib
import json
import os
import struct
from typing import Dict, List, Optional, Set, Tuple

# ---------------------------------------------------------------------------
# 1. RFC 8032 Pure-Python Ed25519 Implementation
# ---------------------------------------------------------------------------
_Q = 2**255 - 19
_L = 2**252 + 27742317777372353535851937790883648493
_D = -121665 * pow(121666, _Q - 2, _Q) % _Q
_I = pow(2, (_Q - 1) // 4, _Q)


def _xrecover(y: int) -> int:
    xx = (y * y - 1) * pow(_D * y * y + 1, _Q - 2, _Q) % _Q
    x = pow(xx, (_Q + 3) // 8, _Q)
    if (x * x - xx) % _Q != 0:
        x = (x * _I) % _Q
    if x % 2 != 0:
        x = _Q - x
    return x


_BY = 4 * pow(5, _Q - 2, _Q) % _Q
_BX = _xrecover(_BY)
_B = (_BX % _Q, _BY % _Q, 1, (_BX * _BY) % _Q)


def _edwards_add(
    P: Tuple[int, int, int, int], Q: Tuple[int, int, int, int]
) -> Tuple[int, int, int, int]:
    x1, y1, z1, t1 = P
    x2, y2, z2, t2 = Q
    a = (y1 - x1) * (y2 - x2) % _Q
    b = (y1 + x1) * (y2 + x2) % _Q
    c = t1 * 2 * _D * t2 % _Q
    d = z1 * 2 * z2 % _Q
    e = (b - a) % _Q
    f = (d - c) % _Q
    g = (d + c) % _Q
    h = (b + a) % _Q
    return (e * f % _Q, g * h % _Q, f * g % _Q, e * h % _Q)


def _scalarmult(
    P: Tuple[int, int, int, int], e: int
) -> Tuple[int, int, int, int]:
    R = (0, 1, 1, 0)
    Q = P
    while e > 0:
        if e & 1:
            R = _edwards_add(R, Q)
        Q = _edwards_add(Q, Q)
        e >>= 1
    return R


def _encodepoint(P: Tuple[int, int, int, int]) -> bytes:
    x, y, z, _ = P
    zi = pow(z, _Q - 2, _Q)
    x = (x * zi) % _Q
    y = (y * zi) % _Q
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def ed25519_publickey(seed32: bytes) -> bytes:
    if len(seed32) != 32:
        raise ValueError(f"Ed25519 seed must be 32 bytes, got {len(seed32)}")
    h = bytearray(hashlib.sha512(seed32).digest())
    h[0] &= 248
    h[31] &= 127
    h[31] |= 64
    a = int.from_bytes(h[:32], "little")
    return _encodepoint(_scalarmult(_B, a))


def ed25519_sign(message: bytes, seed32: bytes, pubkey32: bytes) -> bytes:
    h = bytearray(hashlib.sha512(seed32).digest())
    h[0] &= 248
    h[31] &= 127
    h[31] |= 64
    a = int.from_bytes(h[:32], "little")
    prefix = bytes(h[32:64])
    r = int.from_bytes(hashlib.sha512(prefix + message).digest(), "little") % _L
    R = _encodepoint(_scalarmult(_B, r))
    k = int.from_bytes(hashlib.sha512(R + pubkey32 + message).digest(), "little") % _L
    S = ((r + k * a) % _L).to_bytes(32, "little")
    return R + S


# ---------------------------------------------------------------------------
# 2. Base58 & Compact-u16 Serialization
# ---------------------------------------------------------------------------
_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_MAP = {c: i for i, c in enumerate(_B58_ALPHABET)}


def b58encode(data: bytes) -> str:
    num = int.from_bytes(data, "big")
    out: List[str] = []
    while num > 0:
        num, rem = divmod(num, 58)
        out.append(_B58_ALPHABET[rem])
    pad = len(data) - len(data.lstrip(b"\x00"))
    return "1" * pad + "".join(reversed(out))


def b58decode(s: str) -> bytes:
    num = 0
    for c in s:
        if c not in _B58_MAP:
            return b""
        num = num * 58 + _B58_MAP[c]
    b = num.to_bytes((num.bit_length() + 7) // 8, "big") if num > 0 else b""
    pad = len(s) - len(s.lstrip("1"))
    return b"\x00" * pad + b


def compact_u16(n: int) -> bytes:
    out = bytearray()
    while True:
        elem = n & 0x7F
        n >>= 7
        if n == 0:
            out.append(elem)
            return bytes(out)
        out.append(elem | 0x80)


# ---------------------------------------------------------------------------
# 3. Hard Spend Cap Guard (Enforces <= 0.05 SOL = 50,000,000 lamports)
# ---------------------------------------------------------------------------
class SpendCapExceededError(RuntimeError):
    """Raised whenever a transaction would push cumulative fees past the spend cap."""


class SpendCapGuard:
    """
    Tracks cumulative transaction fees (base fee + priority fee) across the entire
    measurement run and enforces a hard upper bound (default: 0.05 SOL = 50,000,000 lamports).
    """

    def __init__(self, max_spend_lamports: int = 50_000_000):
        if max_spend_lamports <= 0 or max_spend_lamports > 50_000_000:
            raise ValueError(
                f"max_spend_lamports must be in (0, 50_000_000] (<= 0.05 SOL), got {max_spend_lamports}"
            )
        self.max_spend_lamports = int(max_spend_lamports)
        self.spent_lamports = 0
        self.tx_log: List[Dict[str, object]] = []

    @staticmethod
    def compute_exact_fee_lamports(
        num_signatures: int, cu_limit: int, cu_price_micro_lamports: int
    ) -> Tuple[int, int, int]:
        base_fee = 5000 * int(num_signatures)
        # Solana runtime rounds priority fee up: (cu_price * cu_limit + 999_999) // 1_000_000
        prio_fee = (int(cu_price_micro_lamports) * int(cu_limit) + 999_999) // 1_000_000
        total_fee = base_fee + prio_fee
        return base_fee, prio_fee, total_fee

    def reserve(
        self,
        label: str,
        num_signatures: int,
        cu_limit: int,
        cu_price_micro_lamports: int,
    ) -> Dict[str, object]:
        base_fee, prio_fee, total_fee = self.compute_exact_fee_lamports(
            num_signatures, cu_limit, cu_price_micro_lamports
        )
        if self.spent_lamports + total_fee > self.max_spend_lamports:
            raise SpendCapExceededError(
                f"SpendCapGuard blocked '{label}': needed {total_fee} lamports, "
                f"already reserved {self.spent_lamports}/{self.max_spend_lamports} lamports."
            )
        self.spent_lamports += total_fee
        entry = {
            "label": label,
            "base_fee_lamports": base_fee,
            "priority_fee_lamports": prio_fee,
            "total_fee_lamports": total_fee,
            "cumulative_spent_lamports": self.spent_lamports,
            "remaining_budget_lamports": self.max_spend_lamports - self.spent_lamports,
        }
        self.tx_log.append(entry)
        return entry


# ---------------------------------------------------------------------------
# 4. Probe Transaction Builder & Keypair Loader (Env/Volume Only)
# ---------------------------------------------------------------------------
COMPUTE_BUDGET_PROGRAM_ID = "ComputeBudget111111111111111111111111111111"
MEMO_PROGRAM_ID = "Memo1UhkJRfHyvLMcVucJwxXeuD728EqVDDwQDxFMNo"
SYSTEM_PROGRAM_ID = "11111111111111111111111111111111"


def load_keypair_from_env_or_ephemeral() -> Tuple[bytes, bytes, str, bool]:
    """
    Loads a 64-byte Solana keypair array (or 32-byte seed) strictly from environment
    variables or a mounted volume file (`SOLANA_KEYPAIR_JSON` or `SOLANA_KEYPAIR_PATH`).
    In dry-run or devnet modes when no key is provided, derives a deterministic
    ephemeral probe keypair (never committed to repo).
    Returns: (seed32, pubkey32, pubkey_b58, is_ephemeral)
    """
    raw_json = os.environ.get("SOLANA_KEYPAIR_JSON", "").strip()
    kp_path = os.environ.get("SOLANA_KEYPAIR_PATH", "").strip()

    if not raw_json and kp_path and os.path.isfile(kp_path):
        with open(kp_path, "r", encoding="utf-8") as f:
            raw_json = f.read().strip()

    if raw_json:
        arr = json.loads(raw_json)
        if isinstance(arr, list) and len(arr) >= 32:
            seed32 = bytes(arr[:32])
            pk32 = ed25519_publickey(seed32)
            return seed32, pk32, b58encode(pk32), False

    # Deterministic ephemeral probe keypair for dry-run / devnet testing
    seed32 = hashlib.sha256(b"solana-inside-the-block-ephemeral-probe-v1").digest()
    pk32 = ed25519_publickey(seed32)
    return seed32, pk32, b58encode(pk32), True


def build_signed_probe_transaction(
    seed32: bytes,
    pubkey32: bytes,
    writable_target_pubkey32: bytes,
    recent_blockhash_b58: str,
    cu_limit: int,
    cu_price_micro_lamports: int,
    memo_payload: str,
    loaded_accounts_data_size_limit: int = 32768,
) -> Dict[str, object]:
    """
    Constructs and signs a Solana transaction containing:
      1. ComputeBudget::SetComputeUnitLimit(cu_limit)
      2. ComputeBudget::SetComputeUnitPrice(cu_price_micro_lamports)
      3. ComputeBudget::SetLoadedAccountsDataSizeLimit(loaded_accounts_data_size_limit)
      4. Memo instruction locking `writable_target_pubkey32` as a writable account
         so the validator's scheduler (`PrioGraph` / `fd_pack` / `BAM` / `Harmonic`)
         places contending probe transactions into the exact same write-lock conflict set.
    """
    cb_prog = b58decode(COMPUTE_BUDGET_PROGRAM_ID)
    sys_prog = b"\x00" * 32
    memo_prog = b58decode(MEMO_PROGRAM_ID)
    recent_bh = b58decode(recent_blockhash_b58)

    # Account table:
    # [0]: signer + writable (payer)
    # [1]: unsigned + writable (contending probe state lock account)
    # [2]: unsigned + readonly (ComputeBudget program)
    # [3]: unsigned + readonly (SystemProgram)
    # [4]: unsigned + readonly (SPL Memo v1 program)
    header = bytes([1, 0, 3])
    accounts = (
        compact_u16(5)
        + pubkey32
        + writable_target_pubkey32
        + cb_prog
        + sys_prog
        + memo_prog
    )

    ix_cu_lim_data = b"\x02" + struct.pack("<I", int(cu_limit))
    ix_cu_lim = (
        bytes([2])
        + compact_u16(0)
        + compact_u16(len(ix_cu_lim_data))
        + ix_cu_lim_data
    )

    ix_cu_price_data = b"\x03" + struct.pack("<Q", int(cu_price_micro_lamports))
    ix_cu_price = (
        bytes([2])
        + compact_u16(0)
        + compact_u16(len(ix_cu_price_data))
        + ix_cu_price_data
    )

    ix_data_lim_data = b"\x04" + struct.pack("<I", int(loaded_accounts_data_size_limit))
    ix_data_lim = (
        bytes([2])
        + compact_u16(0)
        + compact_u16(len(ix_data_lim_data))
        + ix_data_lim_data
    )

    # 0-lamport SystemProgram::Transfer(from=0, to=1) enforces writable lock on account [1]
    ix_sys_data = struct.pack("<IQ", 2, 0)
    ix_sys = (
        bytes([3])
        + compact_u16(2)
        + bytes([0, 1])
        + compact_u16(len(ix_sys_data))
        + ix_sys_data
    )

    memo_bytes = memo_payload.encode("utf-8")
    ix_memo = (
        bytes([4])
        + compact_u16(1)
        + bytes([1])
        + compact_u16(len(memo_bytes))
        + memo_bytes
    )

    msg = (
        header
        + accounts
        + recent_bh
        + compact_u16(5)
        + ix_cu_lim
        + ix_cu_price
        + ix_data_lim
        + ix_sys
        + ix_memo
    )
    sig = ed25519_sign(msg, seed32, pubkey32)
    raw_tx = compact_u16(1) + sig + msg
    b64_tx = base64.b64encode(raw_tx).decode("ascii")
    sig_b58 = b58encode(sig)

    base_fee, prio_fee, total_fee = SpendCapGuard.compute_exact_fee_lamports(
        1, cu_limit, cu_price_micro_lamports
    )
    # Agave cost model calculation:
    # write_lock_cost = 300 * 2 writable accounts = 600 CUs
    # sig_cost = 720 * 1 signature = 720 CUs
    # data_bytes_cost = len(ix_data) // 32
    # loaded_data_cost = ceil(loaded_accounts_data_size_limit / 32768) * 8
    total_ix_bytes = (
        len(ix_cu_lim_data)
        + len(ix_cu_price_data)
        + len(ix_data_lim_data)
        + len(memo_bytes)
    )
    loaded_cu = ((int(loaded_accounts_data_size_limit) + 32767) // 32768) * 8
    agave_cost_units = (
        int(cu_limit) + 300 * 2 + 720 * 1 + (total_ix_bytes // 32) + loaded_cu
    )
    agave_priority_score = (total_fee * 1_000_000) / (agave_cost_units + 1)
    fd_priority_score = (prio_fee * 1_000_000) / (int(cu_limit) + 1)

    return {
        "signature": sig_b58,
        "raw_tx_base64": b64_tx,
        "raw_tx_bytes": len(raw_tx),
        "signer": b58encode(pubkey32),
        "writable_target": b58encode(writable_target_pubkey32),
        "cu_limit": int(cu_limit),
        "cu_price_micro_lamports": int(cu_price_micro_lamports),
        "loaded_accounts_data_size_limit": int(loaded_accounts_data_size_limit),
        "base_fee_lamports": base_fee,
        "priority_fee_lamports": prio_fee,
        "total_fee_lamports": total_fee,
        "agave_cost_units": agave_cost_units,
        "agave_priority_score": round(agave_priority_score, 2),
        "fd_priority_score": round(fd_priority_score, 2),
        "memo_payload": memo_payload,
    }
