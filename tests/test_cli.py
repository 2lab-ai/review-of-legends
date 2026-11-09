import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from code_review_service import cli, service, llm_runner


class SetupTests(unittest.TestCase):
    def test_private_atomic_write_and_backup_dont_follow_destination_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / 'other'
            target.write_text('keep')
            path = root / 'key'
            path.symlink_to(target)
            cli.private_write(path, 'secret')
            self.assertEqual(target.read_text(), 'keep')
            self.assertFalse(path.is_symlink())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.read_text(), 'secret')

    def test_manifest_has_minimal_permissions_no_webhook(self):
        data = cli.manifest('http://127.0.0.1:1234/callback', 'Fixture Review')
        self.assertEqual(data['name'], 'Fixture Review')
        self.assertEqual(data['default_permissions']['contents'], 'read')
        self.assertEqual(data['default_permissions']['pull_requests'], 'write')
        # Comment-command reactions require Issues write even on a PR.
        self.assertEqual(data['default_permissions']['issues'], 'write')
        self.assertFalse(data['hook_attributes']['active'])
        self.assertFalse(data['public'])

    def test_llmux_exports_are_parsed_not_executed(self):
        values = cli.parse_llmux_env("export ANTHROPIC_BASE_URL=http://localhost:3456\nexport ANTHROPIC_API_KEY='secret $(touch /tmp/no)'\nunset OTHER\n")
        self.assertEqual(values['ANTHROPIC_API_KEY'], 'secret $(touch /tmp/no)')
        self.assertEqual(set(values), {'ANTHROPIC_BASE_URL', 'ANTHROPIC_API_KEY'})

    def test_configuration_is_portable_and_preserves_tuning_on_rerun(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            key = directory / 'app.pem'
            key.write_text('test-only')
            app = dict(app_id=123, installation_id=456, private_key_path=str(key), bot_login='example[bot]')
            config = cli.configuration(directory, ['owner/repo'], app, 'http://localhost:3456', directory / 'key', {})
            path = directory / 'reviewer.json'
            cli.save_json(path, config)
            loaded = service.load_config(path)
            self.assertEqual(loaded['repositories'], ['owner/repo'])
            self.assertFalse(loaded['sweep_legacy_work_dir'])
            # The history store is on: mention commands are polled only with it.
            self.assertTrue(loaded['dashboard']['enabled'])
            self.assertNotIn('public_url', loaded['dashboard'])
            for agent in loaded['agents']:
                self.assertTrue(Path(agent['prompt_path']).is_file())
            self.assertIn('--api-key-file', loaded['agent_runner']['command'])
            self.assertNotIn('--api-key', loaded['agent_runner']['command'])
            self.assertEqual(loaded['agent_runner']['command'][1], str(cli.ROOT / 'code_review_service/llm_runner.py'))
            loaded['max_parallel_agents'] = 4
            loaded['agents'][0]['prompt_path'] = '/custom/prompt.md'
            updated = cli.configuration(directory, ['owner/new'], app, 'https://proxy.example', directory / 'key2', loaded)
            self.assertEqual(updated['max_parallel_agents'], 4)
            self.assertEqual(updated['agents'][0]['prompt_path'], '/custom/prompt.md')
            self.assertEqual(updated['agent_runner']['command'].count('--api-key-file'), 1)

    def test_repo_selection_is_scoped_to_installation_and_clone_identity(self):
        repos = {'repositories': [{'full_name':'o/a'}, {'full_name':'o/b'}]}
        with mock.patch.object(service, 'github_app_token', return_value='test'), mock.patch.object(cli, 'api', return_value=repos), mock.patch.object(cli, 'ask', return_value='2,2'), mock.patch.object(cli, 'run') as run:
            self.assertEqual(cli.choose_repos({}), ['o/b'])
            run.assert_called_once_with(['gh', 'api', 'repos/o/b'], capture=True)
        with mock.patch.object(service, 'github_app_token', return_value='test'), mock.patch.object(cli, 'api', return_value=repos), mock.patch.object(cli, 'ask', return_value='0'):
            with self.assertRaises(service.ServiceError):
                cli.choose_repos({})

    def test_noninteractive_setup_fails_before_mutation(self):
        with mock.patch('sys.stdin.isatty', return_value=False), mock.patch.object(cli, 'private_write') as write:
            self.assertEqual(cli.main(['setup']), 1)
            write.assert_not_called()

    def test_custom_home_cannot_silently_start_default_brew_service(self):
        with mock.patch.dict(os.environ, {'ROL_HOME':'/tmp/custom-rol'}), mock.patch.object(cli, 'run') as run:
            self.assertEqual(cli.main(['start']), 1)
            run.assert_not_called()

    def test_key_file_is_read_without_putting_secret_in_argv(self):
        with tempfile.TemporaryDirectory() as tmp:
            key = Path(tmp) / 'key'
            key.write_text('private-key\n')
            args = ['--prompt-file', 'unused', '--response-file', str(Path(tmp)/'out'), '--model','gpt-test', '--api-key-file', str(key)]
            with mock.patch.object(llm_runner, 'run_with_retries', return_value='done') as runner:
                self.assertEqual(llm_runner.main(args), 0)
                self.assertEqual(runner.call_args.args[0].api_key, 'private-key')
            self.assertNotIn('private-key', args)

    def test_formula_rejects_injection_and_uses_stable_opt_paths(self):
        spec = importlib.util.spec_from_file_location('formula', cli.ROOT/'scripts/homebrew-formula.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        text = module.render('1.2.3', 'a'*64)
        self.assertIn('#{opt_libexec}', text)
        self.assertIn('rol", "daemon', text)
        for version, checksum in [('1.2.3";system("x")', 'a'*64), ('1.2.3', 'oops')]:
            with self.assertRaises(ValueError):
                module.render(version, checksum)

    def test_unconfigured_dashboard_has_no_default_owner_or_viewer_domain(self):
        import subprocess
        env = dict(os.environ)
        for name in ('ROL_DASHBOARD_OWNER', 'ROL_DASHBOARD_VIEWER_DOMAINS', 'ROL_DASHBOARD_PUBLIC_URL'):
            env.pop(name, None)
        result = subprocess.run([__import__('sys').executable, '-c',
            'from code_review_service import dashboard_auth as a; '
            'assert a.ADMIN_EMAIL == ""; assert a.VIEWER_DOMAINS == (); '
            'assert not a.is_owner("owner@company.example")'], cwd=cli.ROOT, env=env, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr.decode())

    def test_setup_backs_up_previous_config_and_validates_before_replacement(self):
        from contextlib import ExitStack
        import subprocess
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            key = directory / 'app.pem'
            key.write_text('fixture')
            app = dict(app_id=123, installation_id=456, private_key_path=str(key), bot_login='example[bot]')
            config = cli.configuration(directory, ['o/a'], app, 'http://localhost:3456', directory/'key', {})
            cli.save_json(directory/'reviewer.json', config)
            original = (directory/'reviewer.json').read_text()
            with ExitStack() as stack:
                stack.enter_context(mock.patch.object(cli, 'home', return_value=directory))
                stack.enter_context(mock.patch('sys.stdin.isatty', return_value=True))
                stack.enter_context(mock.patch.object(cli.shutil, 'which', return_value='/fixture/bin'))
                stack.enter_context(mock.patch.object(cli.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)))
                stack.enter_context(mock.patch.object(cli, 'yes', side_effect=[True, False, False]))
                stack.enter_context(mock.patch.object(cli, 'configure_llmux', return_value=('http://localhost:3456', directory/'key2')))
                stack.enter_context(mock.patch.object(cli, 'configure_app', return_value=app))
                stack.enter_context(mock.patch.object(cli, 'choose_repos', return_value=['o/b']))
                stack.enter_context(mock.patch.object(cli, 'ask', return_value='gpt-test'))
                main = stack.enter_context(mock.patch.object(service, 'main', return_value=0))
                self.assertEqual(cli.setup(), 0)
                self.assertEqual(json.loads((directory/'reviewer.json').read_text())['repositories'], ['o/b'])
                self.assertEqual(next(directory.glob('reviewer.backup-*.json')).read_text(), original)
                main.assert_called_once_with(['--config', str(directory/'reviewer.json'), 'doctor'])

    def test_manifest_callback_rejects_wrong_state_and_saves_registration(self):
        import threading
        import urllib.request
        import urllib.parse
        import urllib.error
        import html
        from concurrent.futures import ThreadPoolExecutor
        outcomes = []
        def browser(url):
            def client():
                with urllib.request.urlopen(url) as response:
                    page = response.read().decode()
                state = page.split('?state=', 1)[1].split('"', 1)[0]
                origin = urllib.parse.urlsplit(url)
                callback = f'{origin.scheme}://{origin.netloc}/callback'
                try:
                    urllib.request.urlopen(callback + '?state=wrong&code=bad')
                except urllib.error.HTTPError as exc:
                    outcomes.append(exc.code)
                with urllib.request.urlopen(callback + '?state=' + state + '&code=fixture-code') as response:
                    outcomes.append(response.status)
            future.append(pool.submit(client))
            return True
        with tempfile.TemporaryDirectory() as tmp, ThreadPoolExecutor(1) as pool:
            future = []
            with mock.patch.object(cli, 'ask', side_effect=['', 'Fixture Review']), mock.patch.object(cli.webbrowser, 'open', side_effect=browser), mock.patch.object(cli, 'api', return_value={'id':123, 'slug':'fixture-app', 'pem':'fixture-private-key'}) as api:
                app = cli.create_app(Path(tmp))
            future[0].result(timeout=5)
            self.assertEqual(outcomes, [400, 200])
            api.assert_called_once_with('/app-manifests/fixture-code/conversions', method='POST')
            self.assertEqual(app['bot_login'], 'fixture-app[bot]')
            self.assertEqual(Path(app['private_key_path']).read_text(), 'fixture-private-key')
            self.assertEqual(Path(app['private_key_path']).stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads((Path(tmp)/'app-registration.json').read_text()), app)

    def test_retired_dashboard_viewer_domain_revokes_existing_session(self):
        from code_review_service import dashboard_auth
        session = {'email':'viewer@example.com', 'sub':'1', 'role':'viewer'}
        with mock.patch.object(dashboard_auth, 'VIEWER_DOMAINS', ('example.com',)):
            self.assertEqual(dashboard_auth.resolve_session(session)['role'], 'viewer')
        with mock.patch.object(dashboard_auth, 'VIEWER_DOMAINS', ()):
            self.assertIsNone(dashboard_auth.resolve_session(session))

    def test_cli_reports_proxy_only_guard_without_traceback(self):
        with mock.patch.object(cli, 'setup', side_effect=llm_runner.RunnerError('proxy required')):
            self.assertEqual(cli.main(['setup']), 1)


    def test_confirmation_enter_preserves_both_defaults(self):
        for default in (False, True):
            with self.subTest(default=default), mock.patch("builtins.input", return_value=""):
                self.assertIs(cli.yes("Continue", default), default)

    def test_confirmation_requires_yes_or_no_and_honors_explicit_answer(self):
        for answer, expected in (("yes", True), ("Y", True), ("no", False), ("N", False)):
            with mock.patch("builtins.input", side_effect=["typo", answer]):
                self.assertIs(cli.yes("Continue", not expected), expected)
