import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest

from tests.test_distribution import ROOT, package


class InstalledSetupTest(unittest.TestCase):
    def test_reuse_and_new_llmux_from_extracted_distribution(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            archive, _ = package.build('0.0.0', directory/'dist')
            with tarfile.open(archive) as tar:
                tar.extractall(directory/'installed')
            runtime = Path(os.environ.get('ROL_ACCEPTANCE_RUNTIME', directory/'installed/review-of-legends-0.0.0'))
            self.assertTrue((runtime/'code_review_service/cli.py').is_file())
            for scenario in ('reuse','new','new-app'):
                with self.subTest(scenario=scenario):
                    sandbox = directory/scenario
                    sandbox.mkdir()
                    result = subprocess.run([sys.executable,str(ROOT/'tests/setup_sandbox.py'),str(runtime),str(sandbox),scenario],
                        cwd=sandbox,text=True,capture_output=True,timeout=45)
                    self.assertEqual(result.returncode,0,result.stdout + result.stderr)
                    self.assertIn('"runner_http_roundtrip": true',result.stdout)
                    self.assertIn('"real_oauth": false',result.stdout)
