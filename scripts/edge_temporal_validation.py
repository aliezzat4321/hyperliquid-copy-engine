#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import statistics
from collections import defaultdict
from pathlib import Path


def load_jsonl(path: Path):
    with path.open() as f:
        for line in f:
            if line.strip():
                yield json.loads(line)


def metrics(rows: list[dict], notional: float) -> dict:
    rows = sorted(rows, key=lambda r: int(r['exchange_ts_ms']))
    pnls = [float(r['net_pnl_usd']) for r in rows]
    if not rows:
        return {'actions': 0, 'net_pnl_usd': 0.0, 'return_bps': 0.0, 'win_rate': 0.0, 'avg_pnl_usd': 0.0, 'median_pnl_usd': 0.0, 'max_drawdown_usd': 0.0, 'trades_per_day': 0.0}
    eq=0.0; peak=0.0; max_dd=0.0
    for p in pnls:
        eq += p
        peak=max(peak,eq)
        max_dd=max(max_dd, peak-eq)
    span_days=max((int(rows[-1]['exchange_ts_ms'])-int(rows[0]['exchange_ts_ms']))/86400000.0, 1/24)
    net=sum(pnls)
    return {
        'actions': len(rows),
        'net_pnl_usd': net,
        'return_bps': net/notional*10000.0 if notional else 0.0,
        'win_rate': sum(1 for p in pnls if p>0)/len(pnls),
        'avg_pnl_usd': statistics.mean(pnls),
        'median_pnl_usd': statistics.median(pnls),
        'max_drawdown_usd': max_dd,
        'max_drawdown_pct_of_notional': max_dd/notional*100.0 if notional else 0.0,
        'trades_per_day': len(rows)/span_days,
        'start_ts_ms': int(rows[0]['exchange_ts_ms']),
        'end_ts_ms': int(rows[-1]['exchange_ts_ms']),
    }


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--funnel-dir', type=Path, default=Path('/root/hyperliquid-audit/funnel'))
    ap.add_argument('--output', type=Path, default=Path('/root/hyperliquid-audit/edge-temporal-validation.json'))
    ap.add_argument('--train-fraction', type=float, default=0.60)
    ap.add_argument('--min-holdout-actions', type=int, default=5)
    args=ap.parse_args()

    report=json.loads((args.funnel_dir/'funnel_report.json').read_text())
    robust=report.get('robust_candidates',[])
    robust_keys={(r['wallet_address'].lower(), r['coin'], str(r['notional_usd'])) for r in robust}

    grouped=defaultdict(list)
    cohort_times=defaultdict(set)
    for r in load_jsonl(args.funnel_dir/'realized_slices.jsonl'):
        key=(str(r['wallet_address']).lower(), str(r['coin']), str(r['notional_usd']))
        if key not in robust_keys: continue
        scenario=str(r['scenario'])
        grouped[key+(scenario,)].append(r)
        cohort_times[key].add(int(r['exchange_ts_ms']))

    configs=[]
    for key in sorted(robust_keys):
        wallet,coin,notional_s=key
        times=sorted(cohort_times.get(key,()))
        if not times: continue
        split_i=min(max(int(len(times)*args.train_fraction),1),len(times)-1) if len(times)>1 else 0
        cutoff=times[split_i] if times else 0
        scenario_rows=[]
        for scenario in sorted({k[3] for k in grouped if k[:3]==key}):
            rows=grouped[key+(scenario,)]
            train=[r for r in rows if int(r['exchange_ts_ms']) < cutoff]
            hold=[r for r in rows if int(r['exchange_ts_ms']) >= cutoff]
            n=float(notional_s)
            scenario_rows.append({'scenario':scenario,'train':metrics(train,n),'holdout':metrics(hold,n)})
        if not scenario_rows: continue
        worst_hold=min(x['holdout']['return_bps'] for x in scenario_rows)
        worst_train=min(x['train']['return_bps'] for x in scenario_rows)
        hold_actions=min(x['holdout']['actions'] for x in scenario_rows)
        worst_dd=max(x['holdout']['max_drawdown_pct_of_notional'] for x in scenario_rows)
        hold_win=min(x['holdout']['win_rate'] for x in scenario_rows)
        passes=worst_hold>0 and hold_actions>=args.min_holdout_actions
        configs.append({
            'wallet_address':wallet,'coin':coin,'notional_usd':notional_s,'cutoff_ts_ms':cutoff,
            'worst_train_return_bps':worst_train,'worst_holdout_return_bps':worst_hold,
            'holdout_actions_floor':hold_actions,'worst_holdout_drawdown_pct':worst_dd,
            'holdout_win_rate_floor':hold_win,'temporal_stability_pass':passes,
            'scenario_metrics':scenario_rows,
        })

    configs.sort(key=lambda r:(r['temporal_stability_pass'],r['worst_holdout_return_bps'],r['holdout_actions_floor']), reverse=True)
    passed=[r for r in configs if r['temporal_stability_pass']]
    out={
        'mode':'POST_SELECTION_TEMPORAL_STABILITY_CHALLENGE_V1',
        'warning':'This is not clean OOS because funnel finalists were selected using the full historical window. Passing candidates require prospective shadow validation.',
        'train_fraction':args.train_fraction,
        'configs_evaluated':len(configs),'configs_passed':len(passed),
        'passed':passed,'all':configs,
    }
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(out,indent=2)+'\n')
    print('=== EDGE TEMPORAL VALIDATION ===')
    print('evaluated=',len(configs),'passed=',len(passed))
    for r in configs[:30]:
        print(f"{'PASS' if r['temporal_stability_pass'] else 'FAIL'} wallet={r['wallet_address'][:14]} coin={r['coin']} notional=${r['notional_usd']} train_worst_bps={r['worst_train_return_bps']:.2f} holdout_worst_bps={r['worst_holdout_return_bps']:.2f} holdout_actions={r['holdout_actions_floor']} dd_pct={r['worst_holdout_drawdown_pct']:.2f} win_floor={r['holdout_win_rate_floor']:.3f}")
    print('report=',args.output)

if __name__=='__main__': main()
