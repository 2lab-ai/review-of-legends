"""Installed-code acceptance probe. Only upstream services and package manager are fakes.

Invoked by test_setup_acceptance in a fresh process and temporary home. Never
imports the checkout's runtime and never touches an existing login or service.
"""
import contextlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock


def main():
    installed, sandbox, scenario = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
    sys.path.insert(0, str(installed))
    # All real network calls are loopback; external APIs are replaced below.
    os.environ['HOME'] = str(sandbox / 'home')
    os.environ.pop('ROL_HOME', None)
    for key in ('GH_TOKEN', 'GITHUB_TOKEN', 'OPENAI_API_KEY', 'ANTHROPIC_API_KEY'):
        os.environ.pop(key, None)
    os.environ['ROL_HOMEBREW'] = '1'
    os.environ['ROL_ASSETS'] = str(installed)
    os.environ['ROL_PYTHON'] = sys.executable
    bindir = sandbox / 'bin'
    bindir.mkdir(parents=True)
    os.environ['PATH'] = str(bindir) + os.pathsep + os.defpath
    events = sandbox / 'commands.jsonl'
    os.environ['ROL_FIXTURE_COMMANDS'] = str(events)
    os.environ['ROL_FIXTURE_BIN'] = str(bindir)
    upstream = []

    class Proxy(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass
        def reply(self, body):
            raw = json.dumps(body).encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        def do_GET(self):
            if self.path == '/v1/models':
                assert self.headers['x-api-key'] == 'fixture-client-key'
            self.reply({'data': [{'id': 'gpt-6-astra[1m]'}]})
        def do_POST(self):
            assert self.path == '/v1/messages'
            assert self.headers['x-api-key'] == 'fixture-client-key'
            payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            upstream.append(payload)
            self.reply({'content': [{'type':'text', 'text':json.dumps({'summary':'fixture review', 'findings':[]})}],
                        'usage':{'input_tokens':10,'output_tokens':10}, 'stop_reason':'end_turn'})

    server = ThreadingHTTPServer(('127.0.0.1', 0), Proxy)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    os.environ['ROL_FIXTURE_ENDPOINT'] = f'http://127.0.0.1:{server.server_port}'
    script = '''#!PYTHON
import json, os, pathlib, shutil, sys
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ['ROL_FIXTURE_COMMANDS'], 'a') as out:
    out.write(json.dumps([name] + args) + '\\n')
if name == 'brew' and args[:1] == ['install']:
    assert args == ['install', '2lab-ai/tap/llmux'], args
    source = pathlib.Path(os.environ['ROL_FIXTURE_BIN']) / 'llmux-template'
    shutil.copy(source, source.with_name('llmux'))
elif name == 'llmux' and args == ['env']:
    print('export ANTHROPIC_BASE_URL=' + os.environ['ROL_FIXTURE_ENDPOINT'])
    print('export ANTHROPIC_API_KEY=fixture-client-key')
elif name == 'llmux' and args == ['login', '--codex']:
    pass
elif name == 'gh' and args == ['auth', 'token']:
    print('fixture-gh-token')
elif name == 'gh' and (args[:1] == ['auth'] or args[:1] == ['api']):
    print('{}')
elif name == 'brew' and args == ['services', 'start', 'review-of-legends']:
    pass
else:
    raise SystemExit('unexpected fixture command: ' + repr([name]+args))
'''.replace('PYTHON', sys.executable, 1)
    for name in ('gh','brew','llmux-template'):
        (bindir/name).write_text(script)
        (bindir/name).chmod(0o755)
    if scenario == 'reuse':
        shutil.copy(bindir/'llmux-template', bindir/'llmux')
    # Generated, disposable RSA material: never registered with any account.
    key = sandbox/'fixture.pem'
    subprocess.run(['openssl','genrsa','-out',str(key),'2048'],check=True,capture_output=True)
    from code_review_service import cli, service
    assert Path(cli.__file__).resolve().is_relative_to(installed.resolve())
    app_answers = ['', '', 'Fixture Review'] if scenario == 'new-app' else ['n', str(key), '123', 'fixture-bot[bot]']
    answers = ([] if scenario == 'reuse' else ['y']) + ['', ''] + app_answers + ['', '', '1', '', '', 'y']
    browser_pool = ThreadPoolExecutor(1)
    callbacks = []
    def browser(url):
        if url.startswith('http://127.0.0.1:'):
            def callback():
                with urllib.request.urlopen(url, timeout=10) as response:
                    page = response.read().decode()
                state = page.split('?state=', 1)[1].split('"', 1)[0]
                parsed = urllib.parse.urlsplit(url)
                target = f'{parsed.scheme}://{parsed.netloc}/callback?state={state}&code=fixture-code'
                with urllib.request.urlopen(target, timeout=10) as response:
                    assert response.status == 200
            callbacks.append(browser_pool.submit(callback))
        return True
    def github(path, token='', method='GET'):
        if path == '/app-manifests/fixture-code/conversions' and method == 'POST':
            return {'id':123,'slug':'fixture-bot','pem':key.read_text()}
        if path.startswith('/app/installations?'):
            return [{'id':456,'account':{'login':'fixture-org'}}]
        if path.startswith('/installation/repositories?'):
            return {'repositories':[{'full_name':'fixture-org/repo'}]}
        raise AssertionError(path)
    try:
        with mock.patch('sys.stdin.isatty', return_value=True), mock.patch('builtins.input', side_effect=answers) as inputs, mock.patch.object(cli.webbrowser,'open',side_effect=browser), mock.patch.object(cli,'api',side_effect=github), mock.patch.object(service,'github_app_token',return_value='fixture-installation-token'):
            assert cli.main(['setup']) == 0
            assert inputs.call_count == len(answers)
        config_path = sandbox/'home/.config/review-of-legends/reviewer.json'
        config = service.load_config(config_path)
        assert config['repositories'] == ['fixture-org/repo']
        assert config['review_existing_open_prs_on_first_run'] is False
        assert config_path.stat().st_mode & 0o777 == 0o600
        assert Path(config['github_app']['private_key_path']).stat().st_mode & 0o777 == 0o600
        key_path = Path(config['agent_runner']['command'][-1])
        assert key_path.read_text() == 'fixture-client-key'
        assert key_path.stat().st_mode & 0o777 == 0o600
        records = [json.loads(line) for line in events.read_text().splitlines()]
        assert (['brew','install','2lab-ai/tap/llmux'] in records) == (scenario != 'reuse')
        assert (['llmux','login','--codex'] in records) == (scenario != 'reuse')
        assert ['brew','services','start','review-of-legends'] in records
        # Run the actual installed runner in a second process, using the generated config.
        prompt, response, meta = sandbox/'prompt', sandbox/'response', sandbox/'meta'
        prompt.write_text('Review the fixture diff: +return 1')
        values = dict(prompt_file=str(prompt), response_file=str(response), meta_file=str(meta), output_contract='json')
        command = service.render_runner_command(config['agent_runner']['command'], values, config['model_policy'])
        result = subprocess.run(command,cwd=sandbox,text=True,capture_output=True,timeout=15)
        assert result.returncode == 0, result.stderr
        assert json.loads(response.read_text())['findings'] == []
        assert upstream and upstream[0]['model'] == 'gpt-6-astra[1m]'
        assert 'fixture-client-key' not in json.dumps(command)
        print(json.dumps({'scenario':scenario,'installed_runtime':True,'setup_saved':True,
            'baseline_default':False,'service_start_dispatched':True,'runner_http_roundtrip':True,
            'manifest_callback':scenario=='new-app','real_oauth':False,'real_github_post':False,'real_service_started':False}))
    finally:
        for callback in callbacks:
            callback.result(timeout=10)
        browser_pool.shutdown()
        server.shutdown()
        server.server_close()
        thread.join()


if __name__ == '__main__':
    main()
