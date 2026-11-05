"""Regression tests for API易 cover requests; no paid API calls."""
import base64
import io
import json
import ssl
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch
from PIL import Image
import cover_service as cover


class CoverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.image = Path(self.temp.name) / 'photo.jpg'
        Image.new('RGB', (400, 600), 'white').save(self.image)
        buf = io.BytesIO()
        Image.new('RGB', (30, 40), 'blue').save(buf, 'PNG')
        self.b64 = base64.b64encode(buf.getvalue()).decode()
        self.context = ssl.create_default_context()
        self.payload = {'image_path': str(self.image), 'prompt': '保持人物不变，调整逆光',
                        'model': 'gemini-3.1-flash-image-preview', 'resolution': '4k', 'aspect_ratio': '3:4'}

    def generate(self, data=None):
        data = data if data is not None else {'candidates': [{'content': {'parts': [
            {'text': 'result'}, {'inlineData': {'data': self.b64, 'mimeType': 'image/png'}}]}}]}
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(data).encode()
        with patch.object(cover.urllib.request, 'urlopen', return_value=response) as call:
            result = cover.generate_cover(self.payload, 'test-key', 'https://api.apiyi.com/v1/', self.context)
        return result, call

    def test_native_request_matches_source_and_uses_trusted_context(self):
        result, call = self.generate()
        request = call.call_args.args[0]
        self.assertEqual(request.full_url, 'https://api.apiyi.com/v1beta/models/gemini-3.1-flash-image-preview:generateContent')
        self.assertEqual(call.call_args.kwargs['context'], self.context)
        self.assertEqual(call.call_args.kwargs['timeout'], 600)
        body = json.loads(request.data)
        self.assertEqual(body['generationConfig'], {'imageConfig': {'imageSize': '4K', 'aspectRatio': '3:4'}})
        self.assertEqual(body['contents'][0]['parts'][0], {'text': self.payload['prompt']})
        self.assertEqual(result['width'], 30)
        self.assertEqual(result['height'], 40)

    def test_base_url_normalizes_pasted_endpoint(self):
        for url in ('https://api.apiyi.com/v1', 'https://api.apiyi.com/v1beta/',
                    'https://api.apiyi.com/v1beta/models/abc:generateContent'):
            self.assertEqual(cover.normalize_base_url(url), 'https://api.apiyi.com')
        with self.assertRaises(cover.CoverError):
            cover.normalize_base_url('http://api.apiyi.com')

    def test_invalid_key_is_actionable_and_redacted(self):
        error = urllib.error.HTTPError('https://api.apiyi.com', 401, 'Unauthorized', {},
                io.BytesIO(b'{"error":{"message":"Invalid token test-key sk-sensitive-example-token"}}'))
        self.addCleanup(error.close)
        with patch.object(cover.urllib.request, 'urlopen', side_effect=error):
            with self.assertRaises(cover.CoverError) as caught:
                cover.generate_cover(self.payload, 'test-key', 'https://api.apiyi.com', self.context)
        self.assertIn('Key', str(caught.exception))
        self.assertNotIn('test-key', str(caught.exception))
        self.assertNotIn('sk-sensitive', str(caught.exception))

    def test_empty_result_is_not_misreported_as_safety_block(self):
        with self.assertRaisesRegex(cover.CoverError, '未返回图片') as caught:
            self.generate({'candidates': [{'finishReason': 'STOP', 'content': {'parts': [{'text': 'done'}]}}]})
        self.assertNotIn('审核拦截', str(caught.exception))

    def test_real_safety_block_reports_reason_without_retry(self):
        with self.assertRaisesRegex(cover.CoverError, '审核拦截'):
            self.generate({'promptFeedback': {'blockReason': 'SAFETY'}})

    def test_corrupt_image_is_not_success(self):
        with self.assertRaisesRegex(cover.CoverError, '图片数据无效'):
            self.generate({'candidates': [{'content': {'parts': [{'inlineData': {'data': 'not-an-image'}}]}}]})

    def test_invalid_model_or_resolution_never_makes_request(self):
        for field, value in [('model', 'unsupported'), ('resolution', '8k')]:
            with self.subTest(field=field), patch.object(cover.urllib.request, 'urlopen') as call:
                with self.assertRaises(cover.CoverError):
                    cover.generate_cover({**self.payload, field: value}, 'test-key', 'https://api.apiyi.com', self.context)
                call.assert_not_called()

    def test_timeout_does_not_automatically_repeat_paid_generation(self):
        with patch.object(cover.urllib.request, 'urlopen', side_effect=TimeoutError('timed out')) as call:
            with self.assertRaisesRegex(cover.CoverError, '超时'):
                cover.generate_cover(self.payload, 'test-key', 'https://api.apiyi.com', self.context)
            self.assertEqual(call.call_count, 1)


class CoverSettingsTests(unittest.TestCase):
    def test_saving_key_preserves_on_empty_password_and_normalizes_base(self):
        import server
        with tempfile.TemporaryDirectory() as folder, patch.object(server, 'DB_PATH', Path(folder) / 'test.sqlite3'):
            server.init_db()
            server.save_cover_settings({'apiyi_key': ' valid-private-key ', 'apiyi_base': 'https://api.apiyi.com/v1'})
            self.assertEqual(server.db_get_setting('apiyi_key'), 'valid-private-key')
            self.assertEqual(server.db_get_setting('apiyi_base'), 'https://api.apiyi.com')
            server.save_cover_settings({'apiyi_key': ''})
            self.assertEqual(server.db_get_setting('apiyi_key'), 'valid-private-key')

    def test_config_route_persists_key_and_invalid_base_does_not_overwrite(self):
        import server
        import threading
        from http.server import ThreadingHTTPServer
        with tempfile.TemporaryDirectory() as folder, patch.object(server, 'DB_PATH', Path(folder) / 'test.sqlite3'):
            server.init_db()
            httpd = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                endpoint = 'http://127.0.0.1:' + str(httpd.server_port) + '/api/config'
                request = cover.urllib.request.Request(endpoint, data=json.dumps({'apiyi_key': 'new-key', 'apiyi_base': 'https://api.apiyi.com/v1'}).encode(), headers={'Content-Type': 'application/json'})
                with cover.urllib.request.urlopen(request) as response:
                    self.assertEqual(response.status, 200)
                self.assertEqual(server.db_get_setting('apiyi_key'), 'new-key')
                request = cover.urllib.request.Request(endpoint, data=json.dumps({'apiyi_key': 'bad-key', 'apiyi_base': 'http://bad'}).encode(), headers={'Content-Type': 'application/json'})
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    cover.urllib.request.urlopen(request)
                caught.exception.close()
                self.assertEqual(server.db_get_setting('apiyi_key'), 'new-key')
            finally:
                httpd.shutdown(); httpd.server_close(); thread.join()


if __name__ == '__main__':
    unittest.main()
