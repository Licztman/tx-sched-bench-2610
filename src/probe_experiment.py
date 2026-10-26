#!/usr/bin/env python3
"""
Controlled 8-Transaction Probe Burst Generator, Dry-Run Simulator, and Live Dispatcher.

Implements three orthogonal experimental arms per validator leader window:
  - Arm 1 (T0..T3 | Contending Write-Lock Priority Inversion Test):
      All 4 transactions lock the same writable account `S_shared`, request `cu_limit=15,000`,
      and are sent in ASCENDING priority order (`cu_price = [1000, 10000, 50000, 200000]`).
      * Pure FIFO / Arrival rule -> lands as [T0, T1, T2, T3] (tau_send = +1.0, tau_prio = -1.0)
      * Priority Scheduler rule  -> inverts to [T3, T2, T1, T0] (tau_prio = +1.0)

  - Arm 2 (T4, T5 | Cost-Unit Denominator vs Raw Fee Discrimination Test):
      Both transactions lock `S_shared`:
      * T4: priority_fee = 2,000 lamports (total_fee = 7,000), cu_limit = 200,000
            -> Agave Priority Score = 34,768 | Firedancer Priority = 10,000
      * T5: priority_fee = 501 lamports (total_fee = 5,501), cu_limit = 1,500
            -> Agave Priority Score = 1,941,758 | Firedancer Priority = 333,777
      * Raw-Fee Scheduler -> T4 lands before T5 (7,000 > 5,501 lamports)
      * Agave/Firedancer Cost-Model Scheduler -> T5 lands strictly before T4!

  - Arm 3 (T6, T7 | Independent Write-Lock Parallel Thread Dispatch Test):
      Lock disjoint writable accounts `S_ind_A` and `S_ind_B` (`write_lock_conflict = False`)
      to isolate multi-thread banking / tile dispatch jitter from single-lock PrioGraph chains.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
import time
import urllib.request
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from ed25519_solana import (
    SpendCapGuard,
    b58encode,
    build_signed_probe_transaction,
    ed25519_publickey,
    load_keypair_from_env_or_ephemeral,
)
from validators_registry import (
    TARGET_VALIDATORS,
    find_validator_leader_windows,
    get_devnet_rpc_url,
    get_mainnet_rpc_url,
    rpc_call,
)


def generate_probe_burst_specs(
    validator_fork_id: str, window_start_slot: int
) -> List[Dict[str, object]]:
    """
    Defines the 8 probe transactions (T0..T7) for a single validator leader window.
    """
    shared_seed = hashlib.sha256(
        f"shared-lock|{validator_fork_id}|{window_start_slot}".encode("utf-8")
    ).digest()
    ind_a_seed = hashlib.sha256(
        f"ind-lock-A|{validator_fork_id}|{window_start_slot}".encode("utf-8")
    ).digest()
    ind_b_seed = hashlib.sha256(
        f"ind-lock-B|{validator_fork_id}|{window_start_slot}".encode("utf-8")
    ).digest()

    s_shared = ed25519_publickey(shared_seed)
    s_ind_a = ed25519_publickey(ind_a_seed)
    s_ind_b = ed25519_publickey(ind_b_seed)

    return [
        # Arm 1: Contending Write-Lock Priority Ladder (sent in ascending fee order)
        {
            "probe_id": "T0_ARM1_LOW",
            "arm": "Arm1_Contending_Ladder",
            "send_seq": 0,
            "writable_target_bytes": s_shared,
            "lock_group": "SHARED_LOCK",
            "cu_limit": 15000,
            "cu_price_micro_lamports": 1000,
        },
        {
            "probe_id": "T1_ARM1_MED",
            "arm": "Arm1_Contending_Ladder",
            "send_seq": 1,
            "writable_target_bytes": s_shared,
            "lock_group": "SHARED_LOCK",
            "cu_limit": 15000,
            "cu_price_micro_lamports": 10000,
        },
        {
            "probe_id": "T2_ARM1_HIGH",
            "arm": "Arm1_Contending_Ladder",
            "send_seq": 2,
            "writable_target_bytes": s_shared,
            "lock_group": "SHARED_LOCK",
            "cu_limit": 15000,
            "cu_price_micro_lamports": 50000,
        },
        {
            "probe_id": "T3_ARM1_MAX",
            "arm": "Arm1_Contending_Ladder",
            "send_seq": 3,
            "writable_target_bytes": s_shared,
            "lock_group": "SHARED_LOCK",
            "cu_limit": 15000,
            "cu_price_micro_lamports": 200000,
        },
        # Arm 2: Cost-Unit Denominator vs Raw Fee Discrimination
        {
            "probe_id": "T4_ARM2_HIGH_RAW_FEE_BLOATED_CU",
            "arm": "Arm2_CostModel_vs_RawFee",
            "send_seq": 4,
            "writable_target_bytes": s_shared,
            "lock_group": "SHARED_LOCK",
            "cu_limit": 200000,
            "cu_price_micro_lamports": 10000,  # prio_fee = 2,000 lamports, total = 7,000
        },
        {
            "probe_id": "T5_ARM2_LOW_RAW_FEE_TIGHT_CU",
            "arm": "Arm2_CostModel_vs_RawFee",
            "send_seq": 5,
            "writable_target_bytes": s_shared,
            "lock_group": "SHARED_LOCK",
            "cu_limit": 1500,
            "cu_price_micro_lamports": 333334,  # prio_fee = 501 lamports, total = 5,501
        },
        # Arm 3: Disjoint Write-Lock Parallel Thread Dispatch
        {
            "probe_id": "T6_ARM3_DISJOINT_A_LOW",
            "arm": "Arm3_Disjoint_Parallel",
            "send_seq": 6,
            "writable_target_bytes": s_ind_a,
            "lock_group": "DISJOINT_A",
            "cu_limit": 15000,
            "cu_price_micro_lamports": 5000,
        },
        {
            "probe_id": "T7_ARM3_DISJOINT_B_HIGH",
            "arm": "Arm3_Disjoint_Parallel",
            "send_seq": 7,
            "writable_target_bytes": s_ind_b,
            "lock_group": "DISJOINT_B",
            "cu_limit": 15000,
            "cu_price_micro_lamports": 150000,
        },
    ]


def discover_upcoming_fork_windows(rpc_url: str, lookahead_slots: int = 800) -> Tuple[List[Dict[str, object]], Dict[str, Dict[str, object]]]:
    """
    Queries Stakewiz validator metadata and `getSlotLeaders` over the next `lookahead_slots`
    to find the earliest upcoming 4-slot leader window for each of the 8 client software forks.
    """
    req = urllib.request.Request(
        "https://api.stakewiz.com/validators", headers={"User-Agent": "Mozilla/5.0"}
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        vals_list = json.loads(r.read().decode("utf-8"))
    vals_by_id = {v["identity"]: v for v in vals_list if v.get("identity")}

    curr_slot = int(rpc_call(rpc_url, "getSlot", [{"commitment": "processed"}])["result"])
    leaders: List[str] = rpc_call(rpc_url, "getSlotLeaders", [curr_slot, lookahead_slots])["result"]

    wanted_forks = {v["fork_id"]: v for v in TARGET_VALIDATORS}
    found: Dict[str, Dict[str, object]] = {}
    last_scheduled_slot = -100

    i = 8
    while i < len(leaders) - 4:
        ident = leaders[i]
        v = vals_by_id.get(ident)
        if v:
            cli = v.get("client")
            w_start = curr_slot + i
            if cli in wanted_forks and cli not in found and (w_start - last_scheduled_slot >= 16):
                w_slots = [w_start + k for k in range(4) if i + k < len(leaders) and leaders[i + k] == ident]
                if len(w_slots) >= 2:
                    found[cli] = {
                        "fork_id": cli,
                        "client_software": wanted_forks[cli]["client_software"],
                        "identity": ident,
                        "name": v.get("name") or wanted_forks[cli]["name"],
                        "version": v.get("version") or wanted_forks[cli]["version"],
                        "is_jito": bool(v.get("is_jito")),
                        "activated_stake_sol": int(float(v.get("activated_stake") or 0)),
                        "target_window_slots": w_slots,
                    }
                    last_scheduled_slot = w_start
        if len(found) == len(wanted_forks):
            break
        i += 1

    ordered = sorted(found.values(), key=lambda x: x["target_window_slots"][0])
    return ordered, vals_by_id


def _send_single_tx_staggered(args: Tuple[List[str], int, str, float]) -> Tuple[int, float, Dict[str, object]]:
    urls, start_idx, raw_b64, delay_sec = args
    if delay_sec > 0:
        time.sleep(delay_sec)
    t_send = time.time()
    for attempt in range(6):
        u = urls[(start_idx + attempt) % len(urls)]
        try:
            res = rpc_call(
                u,
                "sendTransaction",
                [
                    raw_b64,
                    {
                        "encoding": "base64",
                        "skipPreflight": True,
                        "maxRetries": 0,
                    },
                ],
            )
            return (0, t_send, res)
        except Exception as e:
            time.sleep(0.15 * (attempt + 1))
            if attempt == 5:
                return (1, t_send, {"error": str(e)})
    return (1, t_send, {"error": "exhausted"})


def run_probe_experiment(
    mode: str = "dry-run",
    max_spend_lamports: int = 50_000_000,
    accept_mainnet_spend: bool = False,
    output_dir: str = "data/raw",
) -> Dict[str, object]:
    os.makedirs(output_dir, exist_ok=True)
    seed32, pubkey32, pubkey_b58, is_ephemeral = load_keypair_from_env_or_ephemeral()
    spend_guard = SpendCapGuard(max_spend_lamports=max_spend_lamports)

    if mode == "mainnet-live":
        if not accept_mainnet_spend:
            raise RuntimeError(
                "Refusing to broadcast live Mainnet transactions without explicit "
                "--i-accept-mainnet-spend flag."
            )
        if is_ephemeral:
            raise RuntimeError(
                "Refusing to run --mode mainnet-live with an unfunded ephemeral keypair. "
                "Provide SOLANA_KEYPAIR_JSON or SOLANA_KEYPAIR_PATH via env/volume."
            )

    rpc_url = get_devnet_rpc_url() if mode == "devnet-live" else get_mainnet_rpc_url()
    bh_resp = rpc_call(rpc_url, "getLatestBlockhash", [{"commitment": "confirmed"}])
    latest_blockhash = bh_resp["result"]["value"]["blockhash"]
    last_valid_block_height = bh_resp["result"]["value"]["lastValidBlockHeight"]

    validator_runs: List[Dict[str, object]] = []
    vals_by_id: Dict[str, Dict[str, object]] = {}

    if mode == "mainnet-live":
        live_targets, vals_by_id = discover_upcoming_fork_windows(rpc_url, lookahead_slots=800)
        print(f"[MainnetLive] Discovered {len(live_targets)} upcoming fork windows:")
        for lt in live_targets:
            print(f"  - {lt['fork_id']:18s} | window={lt['target_window_slots'][0]}..{lt['target_window_slots'][-1]} | leader={lt['identity']} ({lt['name']})")
    else:
        live_targets = TARGET_VALIDATORS

    for val in live_targets:
        fork_id = str(val["fork_id"])
        identity = str(val["identity"])

        if mode == "dry-run":
            win_info = find_validator_leader_windows(rpc_url, identity)
            target_window = (
                win_info.get("next_upcoming_window")
                or win_info.get("last_completed_window")
                or val["reference_window_slots"]
            )
        elif mode == "mainnet-live":
            target_window = val["target_window_slots"]
            win_info = {"target_window_slots": target_window}
            w_start = int(target_window[0])
            while True:
                c_slot = int(rpc_call(rpc_url, "getSlot", [{"commitment": "processed"}])["result"])
                if c_slot >= w_start - 8:
                    break
                sleep_s = max(0.05, min(1.0, (w_start - 8 - c_slot) * 0.32))
                time.sleep(sleep_s)
            bh_resp = rpc_call(rpc_url, "getLatestBlockhash", [{"commitment": "confirmed"}])
            latest_blockhash = bh_resp["result"]["value"]["blockhash"]
            while True:
                c_slot = int(rpc_call(rpc_url, "getSlot", [{"commitment": "processed"}])["result"])
                if c_slot >= w_start - 4:
                    break
                time.sleep(0.05)
        else:
            ep = rpc_call(rpc_url, "getEpochInfo")["result"]
            target_window = [ep["absoluteSlot"] + k for k in range(4)]
            win_info = {"target_window_slots": target_window}

        window_start_slot = int(target_window[0])
        specs = generate_probe_burst_specs(fork_id, window_start_slot)

        built_probes: List[Dict[str, object]] = []
        raw_b64_list: List[str] = []

        for spec in specs:
            probe_id = str(spec["probe_id"])
            cu_limit = int(spec["cu_limit"])
            cu_price = int(spec["cu_price_micro_lamports"])
            label = f"{fork_id}:{probe_id}@slot_{window_start_slot}"

            budget_entry = spend_guard.reserve(
                label=label,
                num_signatures=1,
                cu_limit=cu_limit,
                cu_price_micro_lamports=cu_price,
            )

            memo_payload = (
                f"ITB_PROBE|auth=9JYemjCz|fork={fork_id}|win={window_start_slot}|"
                f"seq={spec['send_seq']}|id={probe_id}|cu={cu_limit}|price={cu_price}"
            )
            tx_obj = build_signed_probe_transaction(
                seed32=seed32,
                pubkey32=pubkey32,
                writable_target_pubkey32=spec["writable_target_bytes"],
                recent_blockhash_b58=latest_blockhash,
                cu_limit=cu_limit,
                cu_price_micro_lamports=cu_price,
                memo_payload=memo_payload,
            )
            tx_obj["probe_id"] = probe_id
            tx_obj["arm"] = spec["arm"]
            tx_obj["send_seq"] = spec["send_seq"]
            tx_obj["lock_group"] = spec["lock_group"]
            tx_obj["spend_guard"] = budget_entry

            if mode == "dry-run" and spec["send_seq"] in (0, 5):
                sim_res = rpc_call(
                    rpc_url,
                    "simulateTransaction",
                    [
                        tx_obj["raw_tx_base64"],
                        {
                            "encoding": "base64",
                            "sigVerify": True,
                            "replaceRecentBlockhash": False,
                        },
                    ],
                )
                sim_val = sim_res.get("result", {}).get("value", {})
                tx_obj["rpc_sigverify_status"] = (
                    "SIGVERIFY_OK_ACCOUNT_UNFUNDED"
                    if sim_val.get("err") == "AccountNotFound"
                    else ("SIGVERIFY_OK" if sim_val.get("err") is None else str(sim_val.get("err")))
                )
                tx_obj["rpc_simulated_slot"] = (
                    sim_res.get("result", {}).get("context", {}).get("slot")
                )

            raw_b64_list.append(str(tx_obj["raw_tx_base64"]))
            del tx_obj["raw_tx_base64"]
            built_probes.append(tx_obj)

        if mode == "mainnet-live":
            send_urls_env = os.environ.get("SOLANA_SEND_RPC_URLS", "").strip()
            send_urls = (
                [u.strip() for u in send_urls_env.split(",") if u.strip()]
                if send_urls_env
                else [
                    rpc_url,
                    "https://api.mainnet-beta.solana.com",
                    "https://solana-rpc.publicnode.com",
                ]
            )
            tasks = [
                (send_urls, idx, b64, idx * 0.005)
                for idx, b64 in enumerate(raw_b64_list)
            ]
            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(_send_single_tx_staggered, tasks))
            for p_obj, (_, t_sent, s_res) in zip(built_probes, results):
                p_obj["sent_timestamp_unix"] = round(t_sent, 6)
                p_obj["broadcast_result"] = s_res.get("result") or s_res.get("error")
            print(f"[MainnetLive] Dispatched 8-tx burst for {fork_id} at window {window_start_slot}")

        validator_runs.append(
            {
                "fork_id": fork_id,
                "client_software": val["client_software"],
                "validator_identity": identity,
                "validator_name": val["name"],
                "version": val["version"],
                "is_jito": val["is_jito"],
                "leader_schedule_telemetry": win_info,
                "target_window_slots": target_window,
                "probes": built_probes,
            }
        )

    # In mainnet-live mode, wait for confirmation and resolve exact on-chain block indices via getBlock!
    if mode == "mainnet-live":
        print("[MainnetLive] Waiting 6s for block confirmation to verify on-chain landing positions...")
        time.sleep(6.0)
        all_sigs = [p["signature"] for vr in validator_runs for p in vr["probes"]]
        status_map: Dict[str, Dict[str, object]] = {}
        for i in range(0, len(all_sigs), 256):
            batch = all_sigs[i : i + 256]
            st_res = rpc_call(
                rpc_url,
                "getSignatureStatuses",
                [batch, {"searchTransactionHistory": True}],
            )
            vals = st_res.get("result", {}).get("value") or []
            for sig, st in zip(batch, vals):
                if st:
                    status_map[sig] = st

        landing_slots = sorted(list({int(st["slot"]) for st in status_map.values() if st.get("slot")}))
        slot_sig_index: Dict[int, Dict[str, int]] = {}
        slot_total_txs: Dict[int, int] = {}
        for s in landing_slots:
            blk_res = rpc_call(
                rpc_url,
                "getBlock",
                [
                    s,
                    {
                        "encoding": "json",
                        "transactionDetails": "signatures",
                        "maxSupportedTransactionVersion": 1,
                        "rewards": True,
                    },
                ],
            )
            blk = blk_res.get("result")
            if blk:
                sigs_in_blk = blk.get("signatures") or []
                slot_total_txs[s] = len(sigs_in_blk)
                slot_sig_index[s] = {sg: idx for idx, sg in enumerate(sigs_in_blk)}

        for vr in validator_runs:
            for p in vr["probes"]:
                st = status_map.get(p["signature"])
                if st and st.get("slot"):
                    l_slot = int(st["slot"])
                    p["landed_slot"] = l_slot
                    p["execution_err"] = st.get("err")
                    p["confirmation_status"] = st.get("confirmationStatus")
                    p["landed_tx_index"] = slot_sig_index.get(l_slot, {}).get(p["signature"])
                    p["block_total_transactions"] = slot_total_txs.get(l_slot)
                else:
                    p["landed_slot"] = None
                    p["landed_tx_index"] = None

    report = {
        "experiment_timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "mode": mode,
        "post_alpenglow_upgrade": True,
        "alpenglow_upgrade_date": "2026-10-21",
        "probe_signer_pubkey": pubkey_b58,
        "is_ephemeral_signer": is_ephemeral,
        "recent_blockhash": latest_blockhash,
        "last_valid_block_height": last_valid_block_height,
        "spend_cap_summary": {
            "max_spend_lamports": spend_guard.max_spend_lamports,
            "max_spend_sol": spend_guard.max_spend_lamports / 1e9,
            "total_reserved_lamports": spend_guard.spent_lamports,
            "total_reserved_sol": round(spend_guard.spent_lamports / 1e9, 9),
            "utilization_pct": round(
                100.0 * spend_guard.spent_lamports / spend_guard.max_spend_lamports, 4
            ),
            "total_probe_transactions": len(spend_guard.tx_log),
        },
        "validator_runs": validator_runs,
    }

    out_file = os.path.join(output_dir, f"probe_run_{mode.replace('-', '_')}.json")
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)

    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Solana Inside-the-Block Probe Experiment Runner (Dry-Run by Default)"
    )
    parser.add_argument(
        "--mode",
        choices=["dry-run", "devnet-live", "mainnet-live"],
        default="dry-run",
        help="Execution mode (default: dry-run; live sending requires explicit flags).",
    )
    parser.add_argument(
        "--max-spend-lamports",
        type=int,
        default=50_000_000,
        help="Hard cumulative fee cap in lamports (default: 50,000,000 = 0.05 SOL).",
    )
    parser.add_argument(
        "--i-accept-mainnet-spend",
        action="store_true",
        help="Explicit safety gate required when --mode mainnet-live is selected.",
    )
    parser.add_argument(
        "--output-dir",
        default="data/raw",
        help="Directory to write raw probe telemetry JSON.",
    )
    args = parser.parse_args()

    res = run_probe_experiment(
        mode=args.mode,
        max_spend_lamports=args.max_spend_lamports,
        accept_mainnet_spend=args.i_accept_mainnet_spend,
        output_dir=args.output_dir,
    )
    sc = res["spend_cap_summary"]
    print(
        f"[ProbeExperiment] mode={res['mode']} | validators={len(res['validator_runs'])} | "
        f"txs={sc['total_probe_transactions']} | total_fee={sc['total_reserved_lamports']} lamports "
        f"({sc['total_reserved_sol']} SOL / {sc['max_spend_sol']} SOL cap, {sc['utilization_pct']}%)"
    )
