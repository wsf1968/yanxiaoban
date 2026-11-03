"""Run source unit tests with outbound Python socket connections denied."""
from pathlib import Path
import argparse
import json
import os
import socket
import sys
import unittest

ROOT=Path(__file__).resolve().parents[1]
GROUPS={'service':'独立服务','installer':'产品化','secretary':'秘书模块','bookkeeping':'记账模块','clean':'tools'}


def audit(event,args):
    if event=='socket.connect':
        address=args[1]
        if not isinstance(address,tuple) or address[0] not in ('127.0.0.1','::1','localhost'):
            raise RuntimeError('Outbound network is disabled for offline tests')


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('group',choices=GROUPS);args=parser.parse_args()
    for key in list(os.environ):
        if key.startswith(('PAI_','DEEPSEEK_','JUHE_','SECRETARY_EVIDENCE')):
            os.environ.pop(key)
    sys.addaudithook(audit)
    folder=ROOT/GROUPS[args.group]
    sys.path.insert(0,str(folder))
    if args.group == 'service':
        import flights
        # Small, separately licensed fixtures; never the supplier's full tables.
        flights.DATA_PATH = ROOT/'独立服务/tests/flight-locations.fixture.json'
    start=folder if args.group=='clean' else folder/'tests'
    suite=unittest.defaultTestLoader.discover(str(start),pattern='test_*.py')
    result=unittest.TextTestRunner(verbosity=1).run(suite)
    print(json.dumps({'group':args.group,'tests':result.testsRun,'failures':len(result.failures),
        'errors':len(result.errors),'skipped':len(result.skipped),'python_outbound_network':'blocked'}))
    raise SystemExit(not result.wasSuccessful())
