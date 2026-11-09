#!/usr/bin/env python3
"""Exercise real xbrew with a fake Homebrew and temporary HOME. Never installs packages."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def check(binary: Path):
    with tempfile.TemporaryDirectory(prefix="rol-xbrew-") as tmp:
        root = Path(tmp)
        bindir = root/'bin'
        bindir.mkdir()
        fixture = '''#!PYTHON
import json, os, pathlib, sys
args=sys.argv[1:]
root=pathlib.Path(os.environ['ROL_XBREW_SANDBOX'])
with (root/'calls.jsonl').open('a') as out: out.write(json.dumps(args)+'\\n')
state=root/'installed.json'
installed=json.loads(state.read_text()) if state.exists() else []
if args[:1] in (['tap'],['trust']): pass
elif args[:1] == ['info']: pass
elif args[:1] == ['list']:
    if args[-1] in installed: print(args[-1]+' 0.0.0')
elif args[:1] == ['install']:
    installed.append(args[-1]); state.write_text(json.dumps(installed))
elif args[:1] == ['uninstall']:
    installed.remove(args[-1]); state.write_text(json.dumps(installed))
elif args == ['--version']: print('Homebrew fixture')
else: raise SystemExit('unexpected brew command '+repr(args))
'''.replace('PYTHON',sys.executable,1)
        (bindir/'brew').write_text(fixture)
        (bindir/'brew').chmod(0o755)
        env = {**os.environ,'HOME':str(root/'home'),'PATH':str(bindir)+os.pathsep+os.defpath,'ROL_XBREW_SANDBOX':str(root)}
        def run(*args):
            result=subprocess.run([str(binary),*args],env=env,text=True,capture_output=True,timeout=30)
            if result.returncode: raise AssertionError(result.stdout+result.stderr)
        def calls():
            return [json.loads(line) for line in (root/'calls.jsonl').read_text().splitlines()]
        run('bundle',str(ROOT/'packaging/xbrew/software.yaml'))
        recorded=calls()
        for tap in ('2lab-ai/tap',):
            assert recorded.index(['tap',tap]) < recorded.index(['install','llmux'])
            assert recorded.index(['trust','--tap',tap]) < recorded.index(['install','llmux'])
        assert ['install','review-of-legends'] in recorded
        state=json.loads((root/'home/.xbrew/state.json').read_text())['packages']
        assert set(state)=={'llmux','review-of-legends'}
        assert all(value['backend']=='brew' for value in state.values())
        run('bundle',str(ROOT/'packaging/xbrew/software.yaml'))
        assert sum(call[:1]==['install'] for call in calls())==2, 'rerun reinstalled packages'
        run('uninstall','review-of-legends')
        assert ['uninstall','review-of-legends'] in calls()
        if sys.platform=='darwin':
            recipes=root/'home/.xbrew/recipes'
            recipes.mkdir()
            shutil.copy(ROOT/'packaging/xbrew/review-of-legends.toml',recipes)
            run('install','review-of-legends')
            assert ['install','2lab-ai/tap/review-of-legends'] in calls()
            run('uninstall','review-of-legends')
            assert ['uninstall','2lab-ai/tap/review-of-legends'] in calls()
        print(json.dumps({'xbrew_binary':str(binary),'bundle':True,'rerun_adopts':True,
            'uninstall_routes':True,'macos_recipe':sys.platform=='darwin','real_package_install':False}))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--xbrew',type=Path,required=True)
    args=parser.parse_args()
    check(args.xbrew.resolve())
