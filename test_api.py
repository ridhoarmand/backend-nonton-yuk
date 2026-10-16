"""Synthetic fixtures only: these tests never contact a source or a CDN."""

import http.client
import io
import json
import os
import socket
import subprocess
import threading
import unittest
from unittest.mock import Mock, patch

import api

FB_PAGE = 'https://www.facebook.com/watch/?v=123'
FB_HD = 'https://video.example.fbcdn.net/hd.mp4?signature=synthetic'
FB_SD = 'https://video.example.fbcdn.net/sd.mp4?signature=synthetic'
YT_ID = 'jNQXAC9IVRw'
YT_WATCH = f'https://www.youtube.com/watch?v={YT_ID}'
YT_VIDEO = f'https://rr1---sn.synthetic.googlevideo.com/videoplayback?sig=synthetic&id={YT_ID}'
YT_AUDIO = f'https://rr1---sn.synthetic.googlevideo.com/audioplayback?sig=synthetic&id={YT_ID}'


def synthetic_fb(formats=None):
    return {'id': '123', 'title': 'Synthetic video',
            'formats': formats if formats is not None else [
                {'format_id': 'sd', 'url': FB_SD},
                {'format_id': 'hd', 'url': FB_HD}]}


def yt_format(url, vcodec='avc1.64001F', acodec='none', height=1080, fps=30,
              abr=None, ext='mp4', protocol='https', filesize=1000, format_id='dash'):
    fmt = {'format_id': format_id, 'url': url, 'ext': ext, 'protocol': protocol,
           'vcodec': vcodec, 'acodec': acodec, 'height': height, 'fps': fps,
           'tbr': 2000, 'filesize': filesize}
    if abr is not None:
        fmt['abr'] = abr
    return fmt


def synthetic_yt():
    return {'id': YT_ID, 'title': 'Synthetic YouTube', 'duration': 19.0,
            'uploader': 'Synthetic channel', 'thumbnail': 'https://i.ytimg.com/vi/synthetic/hqdefault.jpg',
            'formats': [
                yt_format(YT_VIDEO, vcodec='avc1.64001F', acodec='none', height=1080, fps=30),
                yt_format(YT_AUDIO, vcodec='none', acodec='mp4a.40.2', height=None, fps=None,
                          abr=128, ext='m4a', format_id='audio'),
            ]}


class FakeResponse:
    def __init__(self, status=200, headers=None, body=b'x'):
        self.status = status
        self.headers = headers or {'Content-Type': 'text/html'}
        self.body = io.BytesIO(body)
        self.read_sizes = []

    def getheaders(self):
        return list(self.headers.items())

    def getheader(self, key, default=None):
        return next((value for name, value in self.headers.items() if name.lower() == key.lower()), default)

    def read(self, size):
        self.read_sizes.append(size)
        return self.body.read(size)


class FakeConnection:
    responses = []
    requests = []

    def __init__(self, host, **kwargs):
        self.host = host

    def request(self, method, path, body=None, headers=None):
        self.requests.append((self.host, method, path, body, headers))

    def getresponse(self):
        return self.responses.pop(0)

    def close(self):
        pass


class SourceTests(unittest.TestCase):
    def assert_code(self, code, function, *args, **kwargs):
        with self.assertRaises(api.ApiError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)

    def test_single_link_and_canonical_youtube(self):
        self.assertEqual(api.shared_url(f'Caption {YT_WATCH} done')[1], YT_WATCH)
        self.assertEqual(api.shared_url(f'https://youtu.be/{YT_ID}')[1], YT_WATCH)
        self.assertEqual(api.shared_url(f'https://www.youtube.com/shorts/{YT_ID}?t=5')[1], YT_WATCH)
        # The same link twice is still a single link.
        self.assertEqual(api.shared_url(f'{YT_WATCH} {YT_WATCH}')[1], YT_WATCH)

    def test_duplicate_and_mixed_links_rejected(self):
        other = 'https://www.youtube.com/watch?v=aqz-KE-bpKQ0'
        self.assert_code('invalidUrl', api.shared_url, f'{YT_WATCH} {other}')
        self.assert_code('invalidUrl', api.shared_url, f'{YT_WATCH} {FB_PAGE}')
        self.assert_code('invalidUrl', api.shared_url, 'caption only')
        self.assert_code('invalidUrl', api.shared_url, '')

    def test_youtube_identity_shapes(self):
        self.assertEqual(api.youtube_identity(YT_WATCH), YT_ID)
        for url in (f'https://www.youtube.com/watch?v={YT_ID}&v={YT_ID}',
                    f'https://www.youtube.com/watch?v={YT_ID}&v=aqz-KE-bpKQ0',
                    'https://www.youtube.com/watch?v=short',
                    'https://youtu.be/short',
                    'https://www.youtube.com/shorts/not-an-id/',
                    'https://www.youtube.com/watch/'):
            self.assert_code('invalidUrl', api.youtube_identity, url)

    def test_host_spoof_and_authority_tricks(self):
        for text in ('https://facebook.com.evil.test/watch/?v=123',
                     'https://u:p@www.facebook.com/watch/?v=123',
                     'https://www.facebook.com:8443/watch/?v=123',
                     'https://127.0.0.1/watch/?v=123',
                     'https://[::1]/watch/?v=123',
                     'https://www.youtube.com.evil.test/watch?v=' + YT_ID,
                     'https://u@www.youtube.com/watch?v=' + YT_ID,
                     'https://www.youtube.com:8443/watch?v=' + YT_ID,
                     'file:///etc/passwd',
                     'https://www.facebook.com\\@127.0.0.1/watch/?v=123'):
            with self.subTest(text=text):
                self.assert_code('invalidUrl', api.shared_url, text)

    def test_source_hosts_match_app_registry(self):
        self.assertEqual(set(api.SOURCE_HOSTS), {
            'facebook.com', 'www.facebook.com', 'm.facebook.com', 'web.facebook.com',
            'fb.watch', 'www.fb.watch', 'youtube.com', 'www.youtube.com',
            'm.youtube.com', 'music.youtube.com', 'youtu.be', 'www.youtu.be'})


class DnsTests(unittest.TestCase):
    def assert_code(self, code, function, *args, **kwargs):
        with self.assertRaises(api.ApiError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)

    def test_all_answers_must_be_public(self):
        for address in ('127.0.0.1', '10.0.0.1', '169.254.169.254', '::1', 'fc00::1', '224.0.0.1'):
            answers = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.8.8', 443)),
                       (socket.AF_INET, socket.SOCK_STREAM, 6, '', (address, 443))]
            with self.subTest(address=address), patch('api.socket.getaddrinfo', return_value=answers):
                self.assert_code('blocked', api.public_addresses, 'video.example.fbcdn.net')

    def test_connection_pins_validated_address_and_keeps_tls_hostname(self):
        address = ('8.8.8.8', 443)
        with patch('api.public_addresses', return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, '', address)]), \
                patch('api.socket.socket') as raw_socket:
            connection = api.PublicHTTPSConnection('video.example.fbcdn.net')
            with patch.object(connection._context, 'wrap_socket') as wrap:
                connection.connect()
                raw_socket.return_value.connect.assert_called_once_with(address)
                wrap.assert_called_once_with(raw_socket.return_value, server_hostname='video.example.fbcdn.net')


class TransportTests(unittest.TestCase):
    def assert_code(self, code, function, *args, **kwargs):
        with self.assertRaises(api.ApiError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)

    def test_metadata_content_type_rejected_before_body(self):
        with patch('api.PublicHTTPSConnection', FakeConnection):
            for content_type in ('video/mp4', 'audio/mp4', 'image/jpeg', 'application/octet-stream'):
                response = FakeResponse(200, {'Content-Type': content_type}, b'should not be read')
                FakeConnection.responses = [response]
                self.assert_code('unsupportedMedia', api.Transport().request, FB_PAGE, platform='facebook')
                self.assertEqual(response.read_sizes, [])

    def test_metadata_body_bound(self):
        with patch('api.PublicHTTPSConnection', FakeConnection):
            response = FakeResponse(200, {'Content-Length': str(api.MAX_META_PAGE + 1)}, b'x')
            FakeConnection.responses = [response]
            self.assert_code('unavailable', api.Transport().request, FB_PAGE, platform='facebook')
            FakeConnection.responses = [FakeResponse(200, body=b'x' * (api.MAX_META_PAGE + 1))]
            self.assert_code('unavailable', api.Transport().request, FB_PAGE, platform='facebook')

    def test_redirect_identity_and_downgrade(self):
        with patch('api.PublicHTTPSConnection', FakeConnection):
            FakeConnection.responses = [FakeResponse(302, {'Location': 'https://www.facebook.com/watch/?v=999'})]
            self.assert_code('unavailable', api.Transport().request, FB_PAGE,
                             platform='facebook', expected_id='123')
            FakeConnection.responses = [FakeResponse(302, {'Location': FB_PAGE})]
            self.assert_code('unavailable', api.Transport().request, FB_PAGE, platform='facebook')
            FakeConnection.responses = [FakeResponse(302, {'Location': 'https://evil.test/a'})]
            self.assert_code('blocked', api.Transport().request, FB_PAGE, platform='facebook')
            FakeConnection.responses = [FakeResponse(302, {'Location': 'http://www.facebook.com/watch/?v=123'})]
            self.assert_code('blocked', api.Transport().request, FB_PAGE, platform='facebook')

    def test_urlopen_preserves_json_and_strips_credentials(self):
        transport = api.Transport()
        seen = {}

        def fake_request(url, *, platform, headers=None, method='GET', data=None, expected_id=None):
            seen.update(headers=headers, method=method, data=data)
            return url, b'{}', {}

        with api.GuardedYoutubeDL(transport, 'youtube', YT_ID) as ydl:
            from yt_dlp.networking.common import Request as YdlRequest
            with patch.object(transport, 'request', side_effect=fake_request):
                ydl.urlopen(YdlRequest(YT_WATCH, data=b'{"x":1}',
                                      headers={'Content-Type': 'application/json',
                                               'Cookie': 'secret', 'Authorization': 'Bearer secret'}))
        self.assertEqual(seen['headers']['Content-Type'], 'application/json')
        self.assertNotIn('Cookie', seen['headers'])
        self.assertNotIn('Authorization', seen['headers'])


class SelectionTests(unittest.TestCase):
    def assert_code(self, code, function, *args, **kwargs):
        with self.assertRaises(api.ApiError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)

    def test_pair_beats_lower_muxed_but_tie_prefers_muxed(self):
        info = synthetic_yt()
        result = api.select_streams(info, 'youtube', YT_ID, YT_WATCH)
        self.assertEqual([stream['kind'] for stream in result['streams']], ['video', 'audio'])
        tied = {'id': YT_ID, 'formats': [
            yt_format(YT_VIDEO, vcodec='avc1.64001F', acodec='mp4a.40.2', height=720, fps=30, format_id='muxed'),
            yt_format(YT_VIDEO, vcodec='avc1.64001F', acodec='none', height=720, fps=30),
            yt_format(YT_AUDIO, vcodec='none', acodec='mp4a.40.2', height=None, fps=None,
                      abr=128, ext='m4a', format_id='audio')]}
        result = api.select_streams(tied, 'youtube', YT_ID, YT_WATCH)
        self.assertEqual([stream['kind'] for stream in result['streams']], ['muxed'])

    def test_audio_ranking_and_legacy_facebook(self):
        info = {'id': YT_ID, 'formats': [
            yt_format(YT_VIDEO, height=720, fps=30),
            yt_format(YT_AUDIO, vcodec='none', acodec='mp4a.40.2', height=None, fps=None,
                      abr=64, ext='m4a', format_id='low'),
            yt_format(YT_AUDIO, vcodec='none', acodec='mp4a.40.2', height=None, fps=None,
                      abr=192, ext='m4a', format_id='high')]}
        result = api.select_streams(info, 'youtube', YT_ID, YT_WATCH)
        audios = [stream for stream in result['streams'] if stream['kind'] == 'audio']
        self.assertTrue(audios[0]['parts'][0].startswith('https://'))
        legacy = api.select_streams(synthetic_fb(), 'facebook', '123', FB_PAGE)
        self.assertEqual(legacy['streams'][0]['parts'], [FB_HD])

    def test_drm_live_and_unsupported_codecs(self):
        self.assert_code('unsupportedMedia', api.select_streams,
                         {'id': YT_ID, 'has_drm': True, 'formats': []}, 'youtube', YT_ID, YT_WATCH)
        for info in ({'id': YT_ID, 'is_live': True, 'formats': synthetic_yt()['formats']},
                     {'id': YT_ID, 'live_status': 'is_upcoming', 'formats': synthetic_yt()['formats']},
                     {'id': YT_ID, 'formats': [yt_format(YT_VIDEO, vcodec='vp09.00', acodec='none')]},
                     {'id': YT_ID, 'formats': [yt_format(YT_VIDEO, vcodec='av01.0', acodec='none')]},
                     {'id': YT_ID, 'formats': [yt_format(YT_AUDIO, vcodec='none', acodec='opus',
                                                         height=None, fps=None, ext='webm')]},
                     {'id': YT_ID, 'formats': [{'format_id': 'hls', 'url': YT_VIDEO,
                                                'protocol': 'm3u8_native', 'ext': 'mp4',
                                                'vcodec': 'avc1.64001F', 'acodec': 'none'}]},
                     {'id': YT_ID, 'formats': [{'format_id': 'x', 'url': YT_VIDEO + '.m3u8',
                                                'protocol': 'https', 'ext': 'mp4',
                                                'vcodec': 'avc1.64001F', 'acodec': 'none'}]},
                     {'id': YT_ID, 'formats': []}):
            with self.subTest(info=str(info.get('live_status', info.get('id')))):
                self.assert_code('unsupportedMedia', api.select_streams, info, 'youtube', YT_ID, YT_WATCH)
        self.assert_code('unavailable', api.select_streams,
                         {'id': 'other', 'formats': synthetic_yt()['formats']},
                         'youtube', YT_ID, YT_WATCH)

    def test_fragments_order_path_resolution_and_bounds(self):
        base = 'https://rr1---sn.synthetic.googlevideo.com/base/'
        fmt = {'format_id': 'dash', 'url': base + 'manifest.mpd', 'ext': 'mp4', 'protocol': 'https',
               'vcodec': 'avc1.64001F', 'acodec': 'none', 'height': 720, 'fps': 30,
               'fragment_base_url': base,
               'fragments': [{'path': 'init.mp4'}, {'url': base + 'seg1.m4s'}, {'path': 'seg2.m4s'}]}
        audio = yt_format(YT_AUDIO, vcodec='none', acodec='mp4a.40.2', height=None, fps=None,
                          abr=128, ext='m4a', format_id='audio')
        result = api.select_streams({'id': YT_ID, 'formats': [fmt, audio]}, 'youtube', YT_ID, YT_WATCH)
        self.assertEqual(result['streams'][0]['parts'],
                         [base + 'init.mp4', base + 'seg1.m4s', base + 'seg2.m4s'])
        broken = dict(fmt, fragment_base_url=None,
                      fragments=[{'duration': 2.0}])
        self.assert_code('unsupportedMedia', api.select_streams,
                         {'id': YT_ID, 'formats': [broken, audio]}, 'youtube', YT_ID, YT_WATCH)
        many = dict(fmt, fragments=[{'url': f'{base}seg{i}.m4s'} for i in range(129)])
        self.assert_code('unsupportedMedia', api.select_streams,
                         {'id': YT_ID, 'formats': [many, audio]}, 'youtube', YT_ID, YT_WATCH)

    def test_unknown_filesize_malicious_parts_and_thumbnail(self):
        audio = yt_format(YT_AUDIO, vcodec='none', acodec='mp4a.40.2', height=None, fps=None,
                          abr=128, ext='m4a', format_id='audio', filesize=None)
        del audio['filesize']
        result = api.select_streams(
            {'id': YT_ID, 'thumbnail': 'https://evil.test/thumb.jpg',
             'formats': [yt_format(YT_VIDEO, filesize=None), audio]}, 'youtube', YT_ID, YT_WATCH)
        self.assertIsNone(result['approxBytes'])
        self.assertIsNone(result['thumbnailUrl'])
        evil_video = yt_format('https://u@rr1---sn.synthetic.googlevideo.com/x', filesize=10)
        self.assert_code('unsupportedMedia', api.select_streams,
                         {'id': YT_ID, 'formats': [evil_video, audio]}, 'youtube', YT_ID, YT_WATCH)
        http_video = yt_format('http://rr1---sn.synthetic.googlevideo.com/x', filesize=10)
        self.assert_code('unsupportedMedia', api.select_streams,
                         {'id': YT_ID, 'formats': [http_video, audio]}, 'youtube', YT_ID, YT_WATCH)


class ExtractFlowTests(unittest.TestCase):
    def test_no_cdn_media_request_on_extract(self):
        opened = []
        real_connection = api.PublicHTTPSConnection

        class RecordingConnection(FakeConnection):
            def request(self, method, path, body=None, headers=None):
                opened.append((self.host, headers or {}))
                super().request(method, path, body, headers)

        page = b'<html><head></head><body>synthetic</body></html>'
        with patch('api.PublicHTTPSConnection', RecordingConnection), \
                patch('api.resolve_page', return_value=(YT_WATCH, YT_ID, YT_WATCH)), \
                patch.object(api.GuardedYoutubeDL, 'extract_info', return_value=synthetic_yt()) as extract_info:
            with patch.object(FakeConnection, 'getresponse'):
                pass
            result = api.extract(f'Watch this: {YT_WATCH}')
        extract_info.assert_called_once()
        self.assertEqual(result['platform'], 'youtube')
        self.assertEqual([stream['kind'] for stream in result['streams']], ['video', 'audio'])
        for host, headers in opened:
            self.assertFalse(host.endswith('googlevideo.com'))
            self.assertNotIn('Range', headers)
        self.assertEqual(real_connection, api.PublicHTTPSConnection)

    def test_response_shape_and_bounds(self):
        result = api.select_streams(synthetic_yt(), 'youtube', YT_ID, YT_WATCH)
        body = json.dumps(result, ensure_ascii=False, separators=(',', ':')).encode()
        self.assertLessEqual(len(body), api.MAX_RESPONSE)
        self.assertEqual(set(result), {'platform', 'videoId', 'sourceUrl', 'title', 'uploader',
                                       'durationMs', 'thumbnailUrl', 'height', 'approxBytes', 'streams'})
        kinds = sorted(stream['kind'] for stream in result['streams'])
        self.assertIn(kinds, (['muxed'], ['audio', 'video']))
        for stream in result['streams']:
            self.assertLessEqual(len(stream['parts']), api.MAX_PARTS)
            self.assertEqual(set(stream['headers']), {'User-Agent', 'Referer'})
        long_base = 'https://rr1---sn.synthetic.googlevideo.com/' + 'a' * 2500
        bulky_video = {'format_id': 'dash', 'url': YT_VIDEO, 'ext': 'mp4', 'protocol': 'https',
                       'vcodec': 'avc1.64001F', 'acodec': 'none', 'height': 720, 'fps': 30,
                       'fragment_base_url': long_base + '/',
                       'fragments': [{'path': f'seg{i}.m4s'} for i in range(128)]}
        bulky_audio = yt_format(YT_AUDIO, vcodec='none', acodec='mp4a.40.2', height=None, fps=None,
                                abr=128, ext='m4a', format_id='audio')
        with self.assertRaises(api.ApiError) as caught:
            api.select_streams({'id': YT_ID, 'formats': [bulky_video, bulky_audio]},
                               'youtube', YT_ID, YT_WATCH)
        self.assertEqual(caught.exception.code, 'unsupportedMedia')


@unittest.skipUnless(os.name == 'posix' and subprocess.run(
        ['sh', '-c', 'command -v sleep'], capture_output=True).returncode == 0, 'POSIX sleep required')
class WorkerTests(unittest.TestCase):
    def test_terminate_group_kills_detached_child(self):
        child = subprocess.Popen(['sleep', '30'], start_new_session=True)
        try:
            api.terminate_group(child.pid)
            self.assertIsNotNone(child.wait(timeout=5))
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()


class HttpContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.token = 'synthetic-test-only-not-a-deploy-token'
        cls.server = api.ApiServer(('127.0.0.1', 0), cls.token)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def post(self, body, *, token=None, path='/v1/extract', headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=2)
        request_headers = {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + (token or self.token)}
        request_headers.update(headers or {})
        try:
            connection.request('POST', path, body, request_headers)
            response = connection.getresponse()
            self.assertEqual(response.getheader('Cache-Control'), 'no-store')
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def get(self, path, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=2)
        try:
            connection.request('GET', path, None, headers or {})
            response = connection.getresponse()
            self.assertEqual(response.getheader('Cache-Control'), 'no-store')
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_auth_and_invalid_json_never_extract(self):
        with patch('api.run_extraction') as extract:
            status, body = self.post(b'{}', token='wrong')
            self.assertEqual((status, body['error']['code']), (401, 'unauthorized'))
            for payload in (b'not json', b'{}', b'{"text":123}', b'{"text":"https://evil.test/"}', b'[]'):
                self.assertEqual(self.post(payload)[0], 400)
            self.assertEqual(self.post(b'x' * (api.MAX_BODY + 1))[0], 413)
            self.assertEqual(self.post(b'{}', headers={'Transfer-Encoding': 'chunked'})[0], 400)
            self.assertEqual(self.post(b'{}', headers={'Content-Length': '9' * 5000})[0], 413)
            extract.assert_not_called()

    def test_success_shape_and_busy(self):
        result = api.select_streams(synthetic_yt(), 'youtube', YT_ID, YT_WATCH)
        with patch('api.run_extraction', return_value=result) as extract:
            self.assertEqual(self.post(json.dumps({'text': YT_WATCH})), (200, result))
            extract.assert_called_once_with(YT_WATCH)
            self.server.extractions.acquire()
            try:
                status, body = self.post(json.dumps({'text': YT_WATCH}))
                self.assertEqual((status, body['error']['code']), (503, 'busy'))
            finally:
                self.server.extractions.release()

    def test_health_needs_no_auth_and_ignores_busy_extraction(self):
        with patch('api.run_extraction') as extract:
            self.assertEqual(self.get('/healthz'), (200, {'status': 'ok'}))
            self.server.extractions.acquire()
            try:
                self.assertEqual(self.get('/healthz'), (200, {'status': 'ok'}))
            finally:
                self.server.extractions.release()
            extract.assert_not_called()

    def test_get_elsewhere_stays_invalid_url(self):
        with patch('api.run_extraction') as extract:
            for path in ('/', '/healthz?x=1'):
                status, body = self.get(path)
                self.assertEqual((status, body['error']['code']), (405, 'invalidUrl'))
            extract.assert_not_called()


if __name__ == '__main__':
    unittest.main()
