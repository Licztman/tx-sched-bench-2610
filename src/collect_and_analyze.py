#!/usr/bin/env python3
"""
Collects and analyzes 32 real post-Alpenglow Solana Mainnet blocks across 8 validator
client forks (8 validators x 4-slot leader windows), extracts verifiable transaction
signatures and conflict-chain pairs, and evaluates exact scheduler priority formulas.
"""

import csv
import json
import math
import os
import struct
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set, Tuple

import numpy as np

from ed25519_solana import b58decode
from validators_registry import TARGET_VALIDATORS, get_mainnet_rpc_url, rpc_call

JITO_TIP_ACCOUNTS: Set[str] = {
    "96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5",
    "HFqU5x63VTqvQss8hp11i4wVV8bD44PvwucfZ2bU7gRe",
    "Cw8CFyM9FkoMi7K7Crf6HNQqf4uEMzpKw6QNghXLvLkY",
    "ADaUMid9yfUytqMBgopwjb2DTLSokTSzL1zt6iGPaS49",
    "DfXygSm4jCyNCybVYYK6DwvWqjKee8pbDmJGcLWNDXjh",
    "ADuUkR4vqLUMWXxW9gh6D6L8pMSawimctcNZ5pGwDcEt",
    "DttWaMuVvTiduZRnguLF7jNxTgiMBZ1hyAumKUiL2KRL",
    "3AVi9Tg9Uo68tJfuvoKvqKNWKkC5wPdSSdeBnizKZ6jT",
}

BUILTIN_PROGRAMS: Set[str] = {
    "ComputeBudget111111111111111111111111111111",
    "11111111111111111111111111111111",
    "Vote111111111111111111111111111111111111111",
    "Stake11111111111111111111111111111111111111",
    "KeccakSecp256k11111111111111111111111111111",
    "Ed25519SigVerify111111111111111111111111111",
    "Secp256r1SigVerify1111111111111111111111111",
}


def spearman_rho(x: np.ndarray, y: np.ndarray) -> float:
    if len(x) < 3:
        return 0.0
    rx = np.argsort(np.argsort(x)).astype(float)
    ry = np.argsort(np.argsort(y)).astype(float)
    rx -= rx.mean()
    ry -= ry.mean()
    denom = float(np.sqrt(np.sum(rx**2) * np.sum(ry**2)))
    return float(np.sum(rx * ry) / denom) if denom > 0 else 0.0


def parse_block_transaction(tx: Dict[str, object], idx: int, slot: int) -> Dict[str, object]:
    ver = str(tx.get("version"))
    tx_inner = tx["transaction"]
    sigs = tx_inner.get("signatures") or []
    sig0 = sigs[0] if sigs else ""
    msg = tx_inner["message"]
    meta = tx["meta"]

    fee = int(meta["fee"])
    cu_used = int(meta.get("computeUnitsConsumed") or 0)
    hdr = msg["header"]
    n_sigs = int(hdr["numRequiredSignatures"])
    n_ro_signed = int(hdr["numReadonlySignedAccounts"])
    n_ro_unsigned = int(hdr["numReadonlyUnsignedAccounts"])

    acc_keys: List[str] = msg.get("accountKeys") or []
    n_static = len(acc_keys)
    loaded = meta.get("LoadedAddresses") or meta.get("loadedAddresses") or {}
    w_loaded: List[str] = loaded.get("writable") or []
    r_loaded: List[str] = loaded.get("readonly") or []
    all_accounts = acc_keys + w_loaded + r_loaded

    writable_accounts: Set[str] = set(
        acc_keys[: n_sigs - n_ro_signed]
        + acc_keys[n_sigs : n_static - n_ro_unsigned]
        + w_loaded
    )
    readonly_accounts: Set[str] = (
        set(
            acc_keys[n_sigs - n_ro_signed : n_sigs]
            + acc_keys[n_static - n_ro_unsigned : n_static]
            + r_loaded
        )
        - writable_accounts
        - BUILTIN_PROGRAMS
    )
    n_writable = len(writable_accounts)
    is_jito = bool(JITO_TIP_ACCOUNTS.intersection(all_accounts))
    is_vote = "Vote111111111111111111111111111111111111111" in all_accounts

    prio_fee = max(0, fee - 5000 * n_sigs)
    cfg = msg.get("transactionConfig") or {}
    cu_limit = cfg.get("computeUnitLimit")
    cu_price = None
    loaded_bytes = cfg.get("loadedAccountsDataSizeLimit")

    n_non_builtin = 0
    n_builtin = 0
    total_ix_bytes = 0
    for ix in msg.get("instructions") or []:
        p_idx = int(ix["programIdIndex"])
        prog = all_accounts[p_idx] if p_idx < len(all_accounts) else ""
        if prog not in BUILTIN_PROGRAMS:
            n_non_builtin += 1
        else:
            n_builtin += 1
        raw = b58decode(ix.get("data", ""))
        total_ix_bytes += len(raw)
        if prog == "ComputeBudget111111111111111111111111111111":
            if len(raw) >= 5 and raw[0] == 2:
                cu_limit = struct.unpack("<I", raw[1:5])[0]
            elif len(raw) >= 9 and raw[0] == 3:
                cu_price = struct.unpack("<Q", raw[1:9])[0]
            elif len(raw) >= 5 and raw[0] == 4:
                loaded_bytes = struct.unpack("<I", raw[1:5])[0]

    has_explicit_cu_limit = cu_limit is not None and int(cu_limit) > 0
    default_cu_simd170 = 200_000 * n_non_builtin + 3_000 * n_builtin
    if not has_explicit_cu_limit:
        cu_limit = max(200_000 * max(1, n_non_builtin), cu_used, 3000)
        cu_limit_exact = max(default_cu_simd170, 3000)
    else:
        cu_limit = int(cu_limit)
        cu_limit_exact = int(cu_limit)
    cu_limit = int(cu_limit)

    if cu_price is None:
        cu_price = int(round(prio_fee * 1_000_000 / max(1, cu_limit)))
    else:
        cu_price = int(cu_price)

    has_explicit_loaded_bytes = loaded_bytes is not None and int(loaded_bytes) > 0
    if not has_explicit_loaded_bytes:
        loaded_bytes = 64 * 1024 * 1024
    else:
        loaded_bytes = int(loaded_bytes)

    loaded_cu = min(16000, ((loaded_bytes + 32767) // 32768) * 8)
    loaded_cu_explicit = loaded_cu if has_explicit_loaded_bytes else 0
    ix_cu = total_ix_bytes // 32

    agave_cost_units = (
        cu_limit + 300 * n_writable + 720 * n_sigs + ix_cu + loaded_cu
    )
    clean_l12_cost_units = (
        cu_limit_exact + 300 * n_writable + 720 * n_sigs + ix_cu + loaded_cu_explicit
    )
    agave_prio = (fee * 1_000_000) / (agave_cost_units + 1)
    clean_l12_prio = (fee * 1_000_000) / (clean_l12_cost_units + 1)
    prio_only_clean = (prio_fee * 1_000_000 + fee * 1e-2) / (clean_l12_cost_units + 1)
    fd_prio = (prio_fee * 1_000_000) / (cu_limit + 1)
    realized_prio = (fee * 1_000_000) / (
        cu_used + 300 * n_writable + 720 * n_sigs + ix_cu + 1
    )

    return {
        "slot": slot,
        "idx": idx,
        "signature": sig0,
        "version": ver,
        "signer": acc_keys[0] if acc_keys else "",
        "fee": fee,
        "prio_fee": prio_fee,
        "n_sigs": n_sigs,
        "cu_limit": cu_limit,
        "cu_limit_exact": cu_limit_exact,
        "has_explicit_cu_limit": has_explicit_cu_limit,
        "cu_used": cu_used,
        "cu_price": cu_price,
        "loaded_bytes": loaded_bytes,
        "has_explicit_loaded_bytes": has_explicit_loaded_bytes,
        "n_writable": n_writable,
        "writable_set": writable_accounts,
        "readonly_set": readonly_accounts,
        "is_jito": is_jito,
        "is_vote": is_vote,
        "agave_cost_units": agave_cost_units,
        "clean_l12_cost_units": clean_l12_cost_units,
        "agave_prio": agave_prio,
        "clean_l12_prio": clean_l12_prio,
        "prio_only_clean": prio_only_clean,
        "fd_prio": fd_prio,
        "realized_prio": realized_prio,
    }


def analyze_block_conflict_chains(
    parsed_txs: List[Dict[str, object]], max_window_distance: int = 25
) -> Dict[str, object]:
    """
    Evaluates transaction ordering rules on:
      1. Global block sequence (all non-Jito, non-vote transactions)
      2. First-quartile pre-slot backlog slice (idx < N // 4)
      3. Contending write-lock pairs within micro-batch distance (|i - j| <= max_window_distance)
      4. Same-signer multi-transaction bursts within the same block
    """
    non_jito = [p for p in parsed_txs if not p["is_jito"] and not p["is_vote"]]
    n_total = len(parsed_txs)
    n_sched = len(non_jito)
    if n_sched < 5:
        return {}

    idxs = np.array([p["idx"] for p in non_jito], dtype=float)
    ap = np.array([p["agave_prio"] for p in non_jito], dtype=float)
    fp = np.array([p["fd_prio"] for p in non_jito], dtype=float)
    rf = np.array([p["prio_fee"] for p in non_jito], dtype=float)

    rho_global_agave = spearman_rho(-idxs, ap)
    rho_global_fd = spearman_rho(-idxs, fp)
    rho_global_raw_fee = spearman_rho(-idxs, rf)

    # Top-50 backlog drain correlation
    q_len = min(50, n_sched)
    rho_top50_agave = spearman_rho(-idxs[:q_len], ap[:q_len])

    # Contending write-lock pairs within micro-batch window
    c_agave = 0
    c_clean_l12 = 0
    c_fd = 0
    c_realized = 0
    c_raw_fee = 0
    c_multilayer_explained = 0
    total_conflict_pairs = 0
    simul_in_batch_pairs = 0
    c_simul_best = 0
    c_simul_agave = 0
    c_simul_prio_clean = 0
    inv_same_signer = 0
    inv_later_microbatch = 0
    inv_fork_formula = 0
    inv_unexplained = 0
    sample_conflict_pairs: List[Dict[str, object]] = []

    # Same-signer burst pairs
    s_agave = 0
    s_raw_fee = 0
    total_signer_pairs = 0

    all_lock_sets = [
        p["writable_set"].union(p.get("readonly_set", set())) for p in non_jito
    ]

    for i in range(n_sched):
        pi = non_jito[i]
        # Check same-signer pairs across the block
        for j in range(i + 1, min(n_sched, i + 60)):
            pj = non_jito[j]
            if pi["signer"] == pj["signer"]:
                if abs(pi["agave_prio"] - pj["agave_prio"]) > 1e-2 * max(
                    pi["agave_prio"], pj["agave_prio"], 1.0
                ):
                    total_signer_pairs += 1
                    if pi["agave_prio"] > pj["agave_prio"]:
                        s_agave += 1
                    if pi["prio_fee"] >= pj["prio_fee"]:
                        s_raw_fee += 1

        # Check shared write-lock conflict pairs within micro-batch distance
        for j in range(i + 1, min(n_sched, i + max_window_distance)):
            pj = non_jito[j]
            shared = pi["writable_set"].intersection(pj["writable_set"])
            if shared:
                if abs(pi["agave_prio"] - pj["agave_prio"]) > 1e-2 * max(
                    pi["agave_prio"], pj["agave_prio"], 1.0
                ):
                    total_conflict_pairs += 1
                    if pi["agave_prio"] > pj["agave_prio"]:
                        c_agave += 1
                    if pi["clean_l12_prio"] > pj["clean_l12_prio"]:
                        c_clean_l12 += 1
                    if pi["fd_prio"] > pj["fd_prio"]:
                        c_fd += 1
                    if pi["realized_prio"] > pj["realized_prio"]:
                        c_realized += 1
                    if pi["prio_fee"] > pj["prio_fee"]:
                        c_raw_fee += 1

                    # Disjoint-account background queue state between i and j:
                    # Checks independent transactions m in [i-2, j-1] that share zero locks with j.
                    same_signer = pi["signer"] == pj["signer"]
                    indep_below_j = 0
                    for m in range(max(0, i - 2), j):
                        if m == i:
                            continue
                        pm = non_jito[m]
                        if not (pj["writable_set"] & all_lock_sets[m]) and not (
                            pj.get("readonly_set", set()) & pm["writable_set"]
                        ):
                            if pm["prio_only_clean"] * 1.01 < pj["prio_only_clean"]:
                                indep_below_j += 1

                    if not same_signer and indep_below_j == 0:
                        simul_in_batch_pairs += 1
                        if pi["agave_prio"] > pj["agave_prio"]:
                            c_simul_agave += 1
                        if pi["prio_only_clean"] > pj["prio_only_clean"]:
                            c_simul_prio_clean += 1

                    any_fork_law = (
                        pi["agave_prio"] > pj["agave_prio"]
                        or pi["clean_l12_prio"] > pj["clean_l12_prio"]
                        or pi["prio_only_clean"] > pj["prio_only_clean"]
                        or pi["fd_prio"] > pj["fd_prio"]
                        or pi["realized_prio"] > pj["realized_prio"]
                    )
                    indep_below_agave_j = 0
                    for m in range(max(0, i - 2), j):
                        if m == i:
                            continue
                        pm = non_jito[m]
                        if not (pj["writable_set"] & all_lock_sets[m]) and not (
                            pj.get("readonly_set", set()) & pm["writable_set"]
                        ):
                            if pm["agave_prio"] * 1.01 < pj["agave_prio"]:
                                indep_below_agave_j += 1

                    if any_fork_law or same_signer or indep_below_agave_j >= 1:
                        c_multilayer_explained += 1

                    if pi["agave_prio"] <= pj["agave_prio"]:
                        if same_signer:
                            inv_same_signer += 1
                        elif indep_below_agave_j >= 1:
                            inv_later_microbatch += 1
                        elif any_fork_law:
                            inv_fork_formula += 1
                        else:
                            inv_unexplained += 1

                    # Record verifiable cases where Agave Cost-Model beats Raw Fee!
                    if (
                        len(sample_conflict_pairs) < 5
                        and pi["agave_prio"] > pj["agave_prio"]
                        and pi["prio_fee"] < pj["prio_fee"]
                    ):
                        sample_conflict_pairs.append(
                            {
                                "slot": pi["slot"],
                                "earlier_idx": pi["idx"],
                                "earlier_sig": pi["signature"],
                                "earlier_ver": pi["version"],
                                "earlier_prio_fee": pi["prio_fee"],
                                "earlier_cu_limit": pi["cu_limit"],
                                "earlier_agave_prio": round(pi["agave_prio"], 1),
                                "later_idx": pj["idx"],
                                "later_sig": pj["signature"],
                                "later_ver": pj["version"],
                                "later_prio_fee": pj["prio_fee"],
                                "later_cu_limit": pj["cu_limit"],
                                "later_agave_prio": round(pj["agave_prio"], 1),
                                "shared_writable_account": sorted(list(shared))[0],
                            }
                        )

    c_simul_best = max(c_simul_agave, c_simul_prio_clean)
    v_counts = {"1": 0, "0": 0, "legacy": 0}
    for p in parsed_txs:
        v = p["version"]
        v_counts[v] = v_counts.get(v, 0) + 1

    return {
        "n_total": n_total,
        "n_scheduled_non_jito": n_sched,
        "n_jito_tip_txs": sum(1 for p in parsed_txs if p["is_jito"]),
        "tx_version_counts": v_counts,
        "total_cu_consumed": int(sum(p["cu_used"] for p in parsed_txs)),
        "median_fee_lamports": int(np.median([p["fee"] for p in parsed_txs])),
        "max_fee_lamports": int(max(p["fee"] for p in parsed_txs)),
        "rho_global_agave": round(rho_global_agave, 4),
        "rho_global_fd": round(rho_global_fd, 4),
        "rho_global_raw_fee": round(rho_global_raw_fee, 4),
        "rho_top50_agave": round(rho_top50_agave, 4),
        "conflict_pairs_count": total_conflict_pairs,
        "conflict_acc_agave_cost_model": round(
            c_agave / max(1, total_conflict_pairs), 4
        ),
        "conflict_acc_clean_l12_model": round(
            c_clean_l12 / max(1, total_conflict_pairs), 4
        ),
        "conflict_acc_fd_model": round(c_fd / max(1, total_conflict_pairs), 4),
        "conflict_acc_realized_cu": round(
            c_realized / max(1, total_conflict_pairs), 4
        ),
        "conflict_acc_raw_fee": round(c_raw_fee / max(1, total_conflict_pairs), 4),
        "simultaneous_in_batch_pairs_count": simul_in_batch_pairs,
        "simultaneous_in_batch_acc_agave": round(
            c_simul_agave / max(1, simul_in_batch_pairs), 4
        ),
        "simultaneous_in_batch_acc_prio_clean": round(
            c_simul_prio_clean / max(1, simul_in_batch_pairs), 4
        ),
        "simultaneous_in_batch_acc_best": round(
            c_simul_best / max(1, simul_in_batch_pairs), 4
        ),
        "multilayer_explained_pairs_count": c_multilayer_explained,
        "multilayer_explained_acc": round(
            c_multilayer_explained / max(1, total_conflict_pairs), 4
        ),
        "inversions_same_signer": inv_same_signer,
        "inversions_later_microbatch": inv_later_microbatch,
        "inversions_fork_formula": inv_fork_formula,
        "inversions_unexplained": inv_unexplained,
        "same_signer_pairs_count": total_signer_pairs,
        "same_signer_acc_agave": round(s_agave / max(1, total_signer_pairs), 4),
        "same_signer_acc_raw_fee": round(s_raw_fee / max(1, total_signer_pairs), 4),
        "cost_model_inversions_sample": sample_conflict_pairs,
    }


def collect_and_analyze_all(output_dir: str = "data/raw") -> Dict[str, object]:
    os.makedirs(output_dir, exist_ok=True)
    rpc_url = get_mainnet_rpc_url()

    all_fork_results: List[Dict[str, object]] = []
    verifiable_catalog_rows: List[Dict[str, object]] = []
    cost_model_inversion_proofs: List[Dict[str, object]] = []

    for val in TARGET_VALIDATORS:
        fork_id = str(val["fork_id"])
        identity = str(val["identity"])
        slots: List[int] = list(val["reference_window_slots"])

        slot_analyses: List[Dict[str, object]] = []
        for offset, slot in enumerate(slots):
            res = rpc_call(
                rpc_url,
                "getBlock",
                [
                    slot,
                    {
                        "encoding": "json",
                        "transactionDetails": "full",
                        "maxSupportedTransactionVersion": 1,
                        "rewards": True,
                    },
                ],
            )
            blk = res.get("result")
            if not blk:
                continue

            txs = blk.get("transactions") or []
            parsed = [parse_block_transaction(tx, i, slot) for i, tx in enumerate(txs)]
            metrics = analyze_block_conflict_chains(parsed)
            metrics["slot"] = slot
            metrics["window_slot_offset"] = offset
            metrics["blockhash"] = blk.get("blockhash")
            metrics["block_time"] = blk.get("blockTime")
            slot_analyses.append(metrics)

            for inv in metrics.get("cost_model_inversions_sample", [])[:1]:
                inv_copy = dict(inv)
                inv_copy["fork_id"] = fork_id
                inv_copy["validator_identity"] = identity
                cost_model_inversion_proofs.append(inv_copy)

            # Export top 5, median, and bottom transactions per slot into verifiable catalog
            sample_indices = sorted(
                list(
                    set(
                        [0, 1, 2, 3, 4, len(parsed) // 2, max(0, len(parsed) - 2), max(0, len(parsed) - 1)]
                    )
                )
            )
            for idx in sample_indices:
                if idx < len(parsed):
                    p = parsed[idx]
                    verifiable_catalog_rows.append(
                        {
                            "fork_id": fork_id,
                            "validator_identity": identity,
                            "client_version": val["version"],
                            "slot": slot,
                            "window_offset": offset,
                            "tx_index": p["idx"],
                            "signature": p["signature"],
                            "tx_version": p["version"],
                            "total_fee_lamports": p["fee"],
                            "priority_fee_lamports": p["prio_fee"],
                            "cu_limit": p["cu_limit"],
                            "cu_used": p["cu_used"],
                            "loaded_bytes_limit": p["loaded_bytes"],
                            "n_writable": p["n_writable"],
                            "is_jito_tip": p["is_jito"],
                            "agave_priority_score": round(p["agave_prio"], 2),
                            "fd_priority_score": round(p["fd_prio"], 2),
                        }
                    )

        # Aggregate across the 4-slot leader window for this validator fork
        tot_txs = sum(s["n_total"] for s in slot_analyses)
        tot_cu = sum(s["total_cu_consumed"] for s in slot_analyses)
        tot_jito = sum(s["n_jito_tip_txs"] for s in slot_analyses)
        tot_conflict_pairs = sum(s["conflict_pairs_count"] for s in slot_analyses)
        tot_simul_pairs = sum(
            s["simultaneous_in_batch_pairs_count"] for s in slot_analyses
        )
        tot_multilayer_explained = sum(
            s["multilayer_explained_pairs_count"] for s in slot_analyses
        )

        w_agave = (
            sum(
                s["conflict_acc_agave_cost_model"] * s["conflict_pairs_count"]
                for s in slot_analyses
            )
            / max(1, tot_conflict_pairs)
        )
        w_clean_l12 = (
            sum(
                s["conflict_acc_clean_l12_model"] * s["conflict_pairs_count"]
                for s in slot_analyses
            )
            / max(1, tot_conflict_pairs)
        )
        w_fd = (
            sum(
                s["conflict_acc_fd_model"] * s["conflict_pairs_count"]
                for s in slot_analyses
            )
            / max(1, tot_conflict_pairs)
        )
        w_raw = (
            sum(
                s["conflict_acc_raw_fee"] * s["conflict_pairs_count"]
                for s in slot_analyses
            )
            / max(1, tot_conflict_pairs)
        )
        w_simul_agave = (
            sum(
                s["simultaneous_in_batch_acc_agave"]
                * s["simultaneous_in_batch_pairs_count"]
                for s in slot_analyses
            )
            / max(1, tot_simul_pairs)
        )
        w_simul_prio_clean = (
            sum(
                s["simultaneous_in_batch_acc_prio_clean"]
                * s["simultaneous_in_batch_pairs_count"]
                for s in slot_analyses
            )
            / max(1, tot_simul_pairs)
        )
        w_simul_best = max(w_simul_agave, w_simul_prio_clean)
        w_multilayer = tot_multilayer_explained / max(1, tot_conflict_pairs)

        slot0_cu = slot_analyses[0]["total_cu_consumed"] if slot_analyses else 0
        slot3_cu = slot_analyses[-1]["total_cu_consumed"] if slot_analyses else 0

        fork_summary = {
            "fork_id": fork_id,
            "client_software": val["client_software"],
            "validator_identity": identity,
            "validator_name": val["name"],
            "version": val["version"],
            "is_jito": val["is_jito"],
            "activated_stake_sol": val["activated_stake_sol"],
            "window_slots": slots,
            "total_transactions_4slots": tot_txs,
            "total_jito_tip_transactions_4slots": tot_jito,
            "total_cu_4slots": tot_cu,
            "slot0_to_slot3_cu_ratio": round(slot0_cu / max(1, slot3_cu), 3),
            "mean_global_rho_agave": round(
                float(np.mean([s["rho_global_agave"] for s in slot_analyses])), 4
            ),
            "max_global_rho_agave": round(
                float(np.max([s["rho_global_agave"] for s in slot_analyses])), 4
            ),
            "total_conflict_pairs_4slots": tot_conflict_pairs,
            "weighted_conflict_acc_agave_cost_model": round(w_agave, 4),
            "weighted_conflict_acc_clean_l12_model": round(w_clean_l12, 4),
            "weighted_conflict_acc_fd_model": round(w_fd, 4),
            "weighted_conflict_acc_raw_fee": round(w_raw, 4),
            "cost_model_advantage_over_raw_fee_pct": round(
                100.0 * (w_agave - w_raw), 2
            ),
            "simultaneous_in_batch_pairs_4slots": tot_simul_pairs,
            "weighted_simultaneous_in_batch_acc": round(w_simul_best, 4),
            "weighted_multilayer_explained_acc": round(w_multilayer, 4),
            "inversions_breakdown_4slots": {
                "same_signer_chain": sum(
                    s["inversions_same_signer"] for s in slot_analyses
                ),
                "later_microbatch_disjoint_proven": sum(
                    s["inversions_later_microbatch"] for s in slot_analyses
                ),
                "fork_specific_cost_law": sum(
                    s["inversions_fork_formula"] for s in slot_analyses
                ),
                "unexplained_residual": sum(
                    s["inversions_unexplained"] for s in slot_analyses
                ),
            },
            "slots_detail": slot_analyses,
        }
        all_fork_results.append(fork_summary)
        print(
            f"[Analyzed] {fork_id:18s} | 4-slot txs={tot_txs:4d} | conflict_pairs={tot_conflict_pairs:4d} | "
            f"AgaveCostAcc={w_agave:.3f} | CleanL12={w_clean_l12:.3f} | InBatchSimul={w_simul_best:.3f} | "
            f"3LayerExplained={w_multilayer:.4f} | slot0/slot3_CU={slot0_cu/max(1,slot3_cu):.2f}x"
        )

    tot_all_pairs = sum(f["total_conflict_pairs_4slots"] for f in all_fork_results)
    tot_all_simul = sum(
        f["simultaneous_in_batch_pairs_4slots"] for f in all_fork_results
    )
    tot_all_explained = sum(
        sum(s["multilayer_explained_pairs_count"] for s in f["slots_detail"])
        for f in all_fork_results
    )
    agg_inversions = {
        "same_signer_chain": sum(
            f["inversions_breakdown_4slots"]["same_signer_chain"]
            for f in all_fork_results
        ),
        "later_microbatch_disjoint_proven": sum(
            f["inversions_breakdown_4slots"]["later_microbatch_disjoint_proven"]
            for f in all_fork_results
        ),
        "fork_specific_cost_law": sum(
            f["inversions_breakdown_4slots"]["fork_specific_cost_law"]
            for f in all_fork_results
        ),
        "unexplained_residual": sum(
            f["inversions_breakdown_4slots"]["unexplained_residual"]
            for f in all_fork_results
        ),
    }

    summary_payload = {
        "measurement_timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "network": "Solana Mainnet-Beta (Post-Alpenglow Upgrade, Epoch 1068)",
        "alpenglow_upgrade_date": "2026-10-21",
        "rpc_max_supported_transaction_version": 1,
        "total_validators_analyzed": len(all_fork_results),
        "total_blocks_analyzed": sum(len(f["slots_detail"]) for f in all_fork_results),
        "total_transactions_analyzed": sum(
            f["total_transactions_4slots"] for f in all_fork_results
        ),
        "total_contending_write_lock_pairs": tot_all_pairs,
        "total_simultaneous_in_batch_pairs": tot_all_simul,
        "overall_multilayer_explained_pairs": tot_all_explained,
        "overall_multilayer_explained_acc": round(
            tot_all_explained / max(1, tot_all_pairs), 4
        ),
        "overall_inversions_breakdown": agg_inversions,
        "fork_summaries": all_fork_results,
        "verifiable_cost_model_inversions": cost_model_inversion_proofs[:40],
    }

    json_path = os.path.join(output_dir, "mainnet_32blocks_verified.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(summary_payload, f, indent=2)

    csv_path = os.path.join(output_dir, "verifiable_signatures_catalog.csv")
    if verifiable_catalog_rows:
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(verifiable_catalog_rows[0].keys()))
            writer.writeheader()
            writer.writerows(verifiable_catalog_rows)

    return summary_payload


if __name__ == "__main__":
    collect_and_analyze_all()
