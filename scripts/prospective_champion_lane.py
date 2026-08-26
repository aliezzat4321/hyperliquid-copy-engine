#!/usr/bin/env python3
from __future__ import annotations
import json, os, time
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
WIDE=Path('/mnt/HC_Volume_106576526/hyperliquid/shadow/wide-enriched-live')
MARKET=Path('/mnt/HC_Volume_106576526/hyperliquid/market-shadow')
TARGETS=[
 {'wallet':'0x1081e214bd6f0137234b92432d9b6033b88965b7','coin':'XYZ:KORU','primary_notional':'1000'},
 {'wallet':'0xdb42ab87ac1f9f0d6d83dd82ff49137ab70f631d','coin':'ACE','primary_notional':'5000'},
 {'wallet':'0xeff250ac099ed2e83d2ce0a145bf1bd1587461c8','coin':'XYZ:KIOXIA','primary_notional':'5000'},
 {'wallet':'0x1081e214bd6f0137234b92432d9b6033b88965b7','coin':'XYZ:NOW','primary_notional':'1000'},
]
NOTIONALS=[D('100'),D('1000'),D('5000')]

def atomic(path,payload):
 path.parent.mkdir(parents=True,exist_ok=True); tmp=path.with_suffix('.tmp'); tmp.write_text(json.dumps(payload,indent=2)+'\n'); tmp.replace(path)

def main():
 if os.getenv('REAL_TRADING_ENABLED','NO').upper()=='YES': raise SystemExit('REAL_TRADING_ENABLED must remain NO')
 BASE.mkdir(parents=True,exist_ok=True)
 if not CFG.exists():
  atomic(CFG,{'mode':'FROZEN_CLEAN_PROSPECTIVE_V1','created_ns':time.time_ns(),'targets':TARGETS,'real_trading':False})
 cfg=json.loads(CFG.read_text()); cutoff=int(cfg['created_ns'])
 events=load_wide_events(WIDE,cutoff_ns=cutoff)
 grouped=defaultdict(list)
 for e in events: grouped[(e.wallet_address.lower(),e.coin)].append(e)
 rows=[]
 for t in cfg['targets']:
  ev=tuple(grouped.get((t['wallet'].lower(),t['coin']),[]))
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
 report={'mode':'CLEAN_PROSPECTIVE_CHAMPION_LANE_V1','cutoff_ns':cutoff,'age_hours':(time.time_ns()-cutoff)/3.6e12,'real_trading':False,'targets':rows,'approved_count':sum(1 for r in rows if r['approved'])}
 atomic(REPORT,report)
 print('=== PROSPECTIVE CHAMPIONS ==='); print('age_hours=',round(report['age_hours'],3),'approved=',report['approved_count'])
 for r in rows: print(r['wallet_address'][:14],r['coin'],'events=',r['event_count'],'actions_floor=',r['actions_floor'],'worst_primary_bps=',r['worst_primary_return_bps'],'APPROVED=',r['approved'])
 print('report=',REPORT)
if __name__=='__main__': main()
