#!/usr/bin/env python3
"""Exercise a real, paused launchd service and upgrade on disposable macOS CI."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def run(*args):
    return subprocess.check_output(args, text=True, timeout=180).strip()


def main():
    if os.environ.get('GITHUB_ACTIONS') != 'true' or sys.platform != 'darwin':
        raise SystemExit('Use a disposable macOS GitHub runner only.')
    if os.environ.get('ROL_HOME'):
        raise SystemExit('Lifecycle check requires the disposable runner default home.')
    root = Path(__file__).resolve().parents[1]
    # Installed commands must not import the checkout from Python's cwd entry.
    os.chdir(os.environ['RUNNER_TEMP'])
    home = Path.home() / '.config/review-of-legends'
    if home.exists():
        raise SystemExit('Refusing to replace existing RoL settings.')
    formula = 'rol-ci/tap/review-of-legends'
    opt = Path(run('brew', '--prefix', formula))
    artifacts = Path(os.environ['RUNNER_TEMP']) / 'rol-distribution'
    os.environ['ROL_ASSETS'] = str(opt / 'libexec')
    os.environ['ROL_PYTHON'] = run('brew', '--prefix', 'python@3.13') + '/bin/python3.13'
    sys.path.insert(0, str(opt / 'libexec'))
    from code_review_service import cli, service
    home.mkdir(parents=True, mode=0o700)
    key = home / 'fixture-key'
    cli.private_write(key, 'disposable-fixture-not-a-credential')
    config = cli.configuration(home, ['fixture/lifecycle'], {}, 'http://127.0.0.1:1', key, {})
    config.pop('github_app', None)
    # No account can be accessed, even if the pause guard regresses.
    config['gh_bin'] = '/usr/bin/false'
    config['executor_pause_path'] = str(home / 'paused')
    cli.private_write(home / 'paused', 'CI lifecycle check: never resume')
    cli.save_json(home / 'reviewer.json', config)
    service.load_config(home / 'reviewer.json')
    originals = {p: p.read_bytes() for p in (key, home / 'reviewer.json')}

    def wait_running(expected):
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            info = json.loads(run('brew', 'services', 'info', formula, '--json'))[0]
            if bool(info['running']) == expected:
                return info.get('pid')
            time.sleep(1)
        raise AssertionError(f'service running did not become {expected}: {info}')

    try:
        run('brew', 'services', 'start', formula)
        first_pid = wait_running(True)
        run('brew', 'services', 'stop', formula)
        wait_running(False)
        run('brew', 'services', 'start', formula)
        second_pid = wait_running(True)
        assert first_pid != second_pid
        old_keg = opt.resolve()
        upgraded = artifacts / 'upgrade'
        run(sys.executable, str(root / 'scripts/package-release.py'), '0.0.1', '--out', str(upgraded))
        text = (upgraded / 'review-of-legends.rb').read_text()
        url = 'https://github.com/2lab-ai/review-of-legends/releases/download/v0.0.1/review-of-legends-0.0.1.tar.gz'
        assert url in text
        tap = Path(run('brew', '--repository')) / 'Library/Taps/rol-ci/homebrew-tap/Formula/review-of-legends.rb'
        tap.write_text(text.replace(url, (upgraded / 'review-of-legends-0.0.1.tar.gz').as_uri()))
        print(run('brew', 'upgrade', '--build-from-source', formula), flush=True)
        version = run('rol', 'version')
        print(json.dumps({'version': version, 'opt': str(opt), 'keg': str(opt.resolve()),
                          'version_file': (opt / 'libexec/VERSION').read_text().strip()}), flush=True)
        assert version == 'v0.0.1', version
        assert opt.resolve() != old_keg, (opt, old_keg)
        run('brew', 'services', 'restart', formula)
        upgraded_pid = wait_running(True)
        assert upgraded_pid != second_pid
        run('brew', 'cleanup', '--prune=all', formula)
        assert not old_keg.exists(), old_keg
        for path, content in originals.items():
            assert path.read_bytes() == content
            assert path.stat().st_mode & 0o777 == 0o600
        # Persisted paths still resolve after the old keg has been deleted.
        saved = json.loads((home / 'reviewer.json').read_text())
        for agent in saved['agents']:
            assert Path(agent['prompt_path']).is_file()
        assert Path(saved['agent_runner']['command'][1]).is_file()
        log = Path(run('brew', '--prefix')) / 'var/log/review-of-legends.log'
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if log.exists() and 'executor paused' in log.read_text():
                break
            time.sleep(1)
        else:
            raise AssertionError('daemon did not reach paused scheduler')
        assert wait_running(True) == upgraded_pid
        report = {'real_homebrew_service': True, 'start_stop_restart': True,
                  'upgrade': '0.0.0 -> 0.0.1', 'old_keg_removed': True,
                  'private_config_preserved': True, 'paused_no_live_review': True,
                  'config_sha256': hashlib.sha256(originals[home / 'reviewer.json']).hexdigest()}
        (artifacts / 'lifecycle.json').write_text(json.dumps(report, indent=2) + '\n')
        print(json.dumps(report))
    finally:
        run('brew', 'services', 'stop', formula)
        wait_running(False)


if __name__ == '__main__':
    main()
