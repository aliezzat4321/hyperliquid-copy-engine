#!/usr/bin/env python3
from __future__ import annotations
import argparse, json, os, time
from collections import defaultdict
from decimal import Decimal
from pathlib import Path
from hlcopy.profitability.position_copy import load_wide_events
from hlcopy.profitability.causal_book import CausalParquetL2BookProvider
from hlcopy.profitability.portfolio_position_copy import simulate_copy_with_portfolio_capital
from hlcopy.profitability.position_live_cli import SCENARIOS, _summary

D=Decimal
BASE=Path('/root/hyperliquid-audit/prospective-champions')
CFG=BASE/'config.json'
REPORT=BASE/'report.json'
DEFAULT_QUEUE=Path('/root/hyperliquid-audit/funnel/challenger_queue.json')
WIDE=Path('/mnt/HC_Volume_106576526/hyperliquid/shadow/wide-enriched-live')
MARKET=Path('/mnt/HC_Volume_106576526/hyperliquid/market-shadow')
NOTIONALS=[D('100'),D('1000'),D('5000')]

def atomic(path,payload):
 path.parent.mkdir(parents=True,exist_ok=True); tmp=path.with_suffix('.tmp'); tmp.write_text(json.dumps(payload,indent=2)+'\n'); tmp.replace(path)

def load_frozen_targets(queue_path):
 payload=json.loads(queue_path.read_text())
 targets=[]
 for row in payload.get('candidates',[]):
  if row.get('status')!='challenger': continue
  targets.append({'wallet':str(row['wallet_address']).lower(),'coin':str(row['coin']),'primary_notional':str(row['notional_usd']),'prospective_start_ns':int(row['prospective_start_ns']),'candidate_key':str(row['candidate_key'])})
 return targets

def main():
 if os.getenv('REAL_TRADING_ENABLED','NO').upper()=='YES': raise SystemExit('REAL_TRADING_ENABLED must remain NO')
 ap=argparse.ArgumentParser(); ap.add_argument('--challenger-queue',type=Path,default=DEFAULT_QUEUE); args=ap.parse_args()
 BASE.mkdir(parents=True,exist_ok=True)
 if not args.challenger_queue.exists(): raise SystemExit(f'challenger queue missing: {args.challenger_queue}')
 targets=load_frozen_targets(args.challenger_queue)
 atomic(CFG,{'mode':'AUTONOMOUS_FROZEN_PROSPECTIVE_V2','updated_ns':time.time_ns(),'source_queue':str(args.challenger_queue),'targets':targets,'real_trading':False})
 cutoff=min((int(t['prospective_start_ns']) for t in targets),default=time.time_ns())
 events=load_wide_events(WIDE,cutoff_ns=cutoff)
 grouped=defaultdict(list)
 for e in events: grouped[(e.wallet_address.lower(),e.coin)].append(e)
 rows=[]
 for t in targets:
  ev=tuple(e for e in grouped.get((t['wallet'].lower(),t['coin']),[]) if e.received_at_ns>=int(t['prospective_start_ns']))
  target={'wallet_address':t['wallet'],'coin':t['coin'],'primary_notional':t['primary_notional'],'event_count':len(ev),'scenarios':[]}
  if ev:
   for s in SCENARIOS:
    provider=CausalParquetL2BookProvider(MARKET); provider.prime(ev,(s,))
    for n in NOTIONALS:
     sim=simulate_copy_with_portfolio_capital(ev,provider=provider,scenario=s,notional_usd=n,taker_fee_bps=D('4.5'),max_slippage_bps=D('20'),max_book_forward_ms=750)
     sm=_summary(sim)
     target['scenarios'].append({'scenario':s.name,'notional_usd':str(n),'realized_actions':int(sm['realized_actions']),'net_return_bps':str(sm['net_return_bps']),'closed_net_pnl_usd':str(sm['closed_net_pnl_usd'])})
  primary=[r for r in target['scenarios'] if r['notional_usd']==t['primary_notional']]
  if primary:
   target['worst_primary_return_bps']=str(min(D(r['net_return_bps']) for r in primary)); target['actions_floor']=min(r['realized_actions'] for r in primary)
   target['approved']=target['actions_floor']>=20 and D(target['worst_primary_return_bps'])>0
  else:
   target['worst_primary_return_bps']=None; target['actions_floor']=0; target['approved']=False
  rows.append(target)
 report={'mode':'AUTONOMOUS_CLEAN_PROSPECTIVE_LANE_V2','cutoff_ns':cutoff,'age_hours':(time.time_ns()-cutoff)/3.6e12,'real_trading':False,'targets':rows,'challenger_count':len(targets),'prospective_shadow_count':sum(1 for r in rows if r['event_count']>0),'approved_count':sum(1 for r in rows if r['approved']),'rejections':[] if targets else [{'reason':'NO_ACTIVE_CHALLENGERS','timestamp_ns':time.time_ns()}]}
 atomic(REPORT,report)
 print('=== PROSPECTIVE CHAMPIONS ==='); print('age_hours=',round(report['age_hours'],3),'approved=',report['approved_count'])
 for r in rows: print(r['wallet_address'][:14],r['coin'],'events=',r['event_count'],'actions_floor=',r['actions_floor'],'worst_primary_bps=',r['worst_primary_return_bps'],'APPROVED=',r['approved'])
 print('report=',REPORT)
if __name__=='__main__': main()
