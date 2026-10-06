import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from openfrp_client import OpenFrpClient, validate_settings


class OpenFrpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.exe = self.root / 'frpc with spaces.exe'
        self.exe.write_bytes(b'fake')
        self.config = self.root / 'proxy.toml'
        self.config.write_text('secret configuration')
        self.value = dict(executable=str(self.exe), config=str(self.config), url='https://photos.example.com:8443/')

    def tearDown(self):
        self.temp.cleanup()

    def test_config_validation_and_missing_files(self):
        self.assertEqual(validate_settings(self.value)['url'], 'https://photos.example.com:8443')
        self.assertEqual(validate_settings(dict(self.value, url='http://node.example.com:50533/'))['url'], 'http://node.example.com:50533')
        for url in ['ftp://example.com', 'http://example.com:99999', 'https://example.com/a/token', 'https://user:pass@example.com']:
            with self.assertRaises(ValueError):
                validate_settings(dict(self.value, url=url))
        with self.assertRaises(ValueError):
            validate_settings(dict(self.value, executable=str(self.root/'missing.exe')))

    def test_start_is_owned_hidden_and_not_repeated(self):
        process = Mock()
        process.poll.return_value = None
        process.stdout = io.BytesIO(b'')
        with patch('openfrp_client.SETTINGS', self.root/'settings.json'):
            client = OpenFrpClient()
            client.save(self.value)
            with patch('openfrp_client.subprocess.Popen', return_value=process) as launch, patch('openfrp_client.threading.Thread'):
                self.assertTrue(client.start())
                self.assertFalse(client.start())
                self.assertEqual(launch.call_args.args[0], [str(self.exe), '--config', str(self.config)])
                self.assertNotIn('shell', launch.call_args.kwargs)
                client.stop()
                process.terminate.assert_called_once()
            stored = (self.root/'settings.json').read_text()
            self.assertNotIn('secret configuration', stored)


if __name__ == '__main__':
    unittest.main()
