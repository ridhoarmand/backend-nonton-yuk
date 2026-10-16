"""Authenticated URL-only extraction; the client transfers the video.

Backend never fetches media bytes: it resolves one shared link, reads only
metadata/manifest pages, and returns CDN descriptors the app downloads.
"""

import hashlib
import hmac
import http.client
import io
import ipaddress
import json
import multiprocessing
import os
import re
import signal
import socket
import ssl
import threading
import time
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urljoin, urlsplit, urlunsplit

from yt_dlp import YoutubeDL
from yt_dlp.extractor.facebook import FacebookIE as BaseFacebookIE
from yt_dlp.extractor.youtube import YoutubeIE as BaseYoutubeIE
from yt_dlp.networking.common import Request, Response
from yt_dlp.networking.exceptions import HTTPError
from yt_dlp.utils import DownloadError

SOURCE_HOSTS = {
    'facebook.com': 'facebook',
    'www.facebook.com': 'facebook',
    'm.facebook.com': 'facebook',
    'web.facebook.com': 'facebook',
    'fb.watch': 'facebook',
    'www.fb.watch': 'facebook',
    'youtube.com': 'youtube',
    'www.youtube.com': 'youtube',
    'm.youtube.com': 'youtube',
    'music.youtube.com': 'youtube',
    'youtu.be': 'youtube',
    'www.youtu.be': 'youtube',
}
# Hosts the extractor may read metadata/manifests from, per platform.
META_SUFFIXES = {
    'facebook': ('facebook.com', 'fb.watch', 'fbcdn.net'),
    'youtube': ('youtube.com', 'googlevideo.com'),
}
META_EXACT = {
    'facebook': frozenset(),
    'youtube': frozenset({'youtubei.googleapis.com', 'consent.google.com'}),
}
# Suffixes accepted for resulting CDN media descriptors, per platform.
MEDIA_SUFFIXES = {
    'facebook': ('facebook.com', 'fbcdn.net'),
    'youtube': ('googlevideo.com',),
}
# Hosts accepted for result thumbnails.
THUMB_SUFFIXES = ('ytimg.com', 'fbcdn.net', 'facebook.com')
RESULT_HEADERS = {'user-agent', 'referer', 'origin', 'accept', 'accept-language'}
PAGE_HEADERS = {'User-Agent': 'Mozilla/5.0', 'Accept': 'text/html',
                'Accept-Language': 'en-US,en;q=0.9', 'Accept-Encoding': 'identity'}
MAX_BODY = 16 * 1024
MAX_META_PAGE = 8 * 1024 * 1024
MAX_PART_URL = 16 * 1024
MAX_PARTS = 128
MAX_RESPONSE = 256 * 1024
DEADLINE = 45
YOUTUBE_ID = re.compile(r'[A-Za-z0-9_-]{11}\Z')
MESSAGES = {
    'invalidUrl': 'Masukkan tepat satu tautan video yang valid.',
    'unavailable': 'Video ini tidak tersedia atau memerlukan login.',
    'blocked': 'Sumber atau CDN membatasi akses video ini.',
    'unsupportedMedia': 'Video ini tidak menyediakan format MP4 (H.264/AAC) yang didukung.',
    'network': 'Tidak dapat menghubungi sumber video. Coba lagi.',
    'busy': 'Server sedang memeriksa video lain. Coba lagi sebentar.',
    'unauthorized': 'Token API tidak valid.',
}


class ApiError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(MESSAGES[code])


def error_result(code):
    return {'error': {'code': code, 'message': MESSAGES[code]}}


def suffix_match(host, suffixes):
    return any(host == suffix or host.endswith('.' + suffix) for suffix in suffixes)


def split_url(url, *, limit=MAX_BODY):
    if not isinstance(url, str) or len(url) > limit or re.search(r'[\x00-\x20\x7f\\]', url):
        raise ValueError
    parts = urlsplit(url)
    if parts.username is not None or parts.password is not None:
        raise ValueError
    host = (parts.hostname or '').lower()
    if not host or '%' in parts.netloc or ':' in host:
        raise ValueError
    try:
        ipaddress.ip_address(host)
        raise ValueError
    except ValueError as exc:
        if 'not an ip' not in str(exc).lower() and 'does not appear' not in str(exc).lower():
            raise
    return parts, host


def checked_meta_url(url, platform):
    try:
        parts, host = split_url(url)
        if parts.scheme != 'https' or parts.port not in (None, 443):
            raise ValueError
        if parts.netloc.lower() != host:
            raise ValueError
        if host in META_EXACT[platform] or suffix_match(host, META_SUFFIXES[platform]):
            return urlunsplit(('https', host, parts.path or '/', parts.query, ''))
        raise ValueError
    except (ValueError, TypeError, KeyError):
        raise ApiError('blocked') from None


def checked_media_url(url, platform):
    try:
        parts, host = split_url(url, limit=MAX_PART_URL)
        if parts.scheme != 'https' or parts.port not in (None, 443):
            raise ValueError
        if parts.netloc.lower() != host or not suffix_match(host, MEDIA_SUFFIXES[platform]):
            raise ValueError
        return urlunsplit(('https', host, parts.path or '/', parts.query, ''))
    except (ValueError, TypeError, KeyError):
        raise ApiError('unsupportedMedia') from None


def facebook_identity(url):
    parts = urlsplit(url)
    host, path = parts.hostname, parts.path
    if host in ('fb.watch', 'www.fb.watch'):
        if re.fullmatch(r'/[A-Za-z0-9_-]+/?', path):
            return None
        raise ApiError('invalidUrl')
    if path.rstrip('/') in ('/watch', '/video.php', '/video/video.php'):
        ids = parse_qs(parts.query).get('v', [])
        if len(ids) == 1 and re.fullmatch(r'[0-9]+', ids[0]):
            return ids[0]
    else:
        match = re.fullmatch(r'/(?:reel/|[^/]+/videos/(?:[^/]+/)?)([0-9]+)/?', path)
        if match:
            return match[1]
        if re.fullmatch(r'/share/[vr]/[A-Za-z0-9_-]+/?', path):
            return None
    raise ApiError('invalidUrl')


def youtube_identity(url):
    parts = urlsplit(url)
    host, path = parts.hostname, parts.path.rstrip('/') or '/'
    if host in ('youtu.be', 'www.youtu.be'):
        match = re.fullmatch(r'/([A-Za-z0-9_-]{11})', path)
        if match:
            return match[1]
    elif host in ('youtube.com', 'www.youtube.com', 'm.youtube.com', 'music.youtube.com'):
        if path == '/watch':
            ids = parse_qs(parts.query).get('v', [])
            if len(ids) == 1 and YOUTUBE_ID.fullmatch(ids[0]):
                return ids[0]
        else:
            match = re.fullmatch(r'/(?:shorts|live|embed)/([A-Za-z0-9_-]{11})', path)
            if match:
                return match[1]
    raise ApiError('invalidUrl')


def canonical_source(platform, url):
    if platform == 'youtube':
        return f'https://www.youtube.com/watch?v={youtube_identity(url)}'
    return url


def shared_url(text):
    if not isinstance(text, str) or len(text.encode('utf-8')) > MAX_BODY:
        raise ApiError('invalidUrl')
    found = set()
    for candidate in re.findall(r'https?://[^\s<>"\x27]+', text, re.I):
        candidate = candidate.rstrip('.,!?:;)]}')
        try:
            parts = urlsplit(candidate)
        except ValueError:
            continue
        if parts.scheme not in ('http', 'https'):
            continue
        host = (parts.hostname or '').lower()
        platform = SOURCE_HOSTS.get(host)
        if platform is None:
            continue
        if parts.port not in (None, 80, 443):
            raise ApiError('invalidUrl')
        try:
            _, clean_host = split_url(candidate)
        except ValueError:
            raise ApiError('invalidUrl')
        normalized = urlunsplit(('https', clean_host, parts.path or '/', parts.query, ''))
        if platform == 'facebook':
            facebook_identity(normalized)
        else:
            youtube_identity(normalized)
        found.add((platform, canonical_source(platform, normalized)))
    if len(found) != 1:
        raise ApiError('invalidUrl')
    platform, source = found.pop()
    identity = facebook_identity(source) if platform == 'facebook' else youtube_identity(source)
    return platform, source, identity


def public_addresses(host, port=443):
    addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    ips = [ipaddress.ip_address(item[4][0]) for item in addresses]
    if not ips or any(not ip.is_global or ip.is_multicast or ip.is_reserved for ip in ips):
        raise ApiError('blocked')
    return addresses


class PublicHTTPSConnection(http.client.HTTPSConnection):
    def connect(self):
        # Connect to the validated address itself, not a second DNS lookup (rebinding).
        addresses = public_addresses(self.host, self.port)
        last_error = None
        for family, kind, protocol, _, address in addresses:
            raw = socket.socket(family, kind, protocol)
            raw.settimeout(20)
            try:
                raw.connect(address)
                self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
                return
            except OSError as exc:
                raw.close()
                last_error = exc
        raise last_error or OSError('No reachable public address')


# Hop-by-hop and credential headers are never forwarded to origins.
STRIP_HEADERS = frozenset({
    'cookie', 'authorization', 'proxy-authorization', 'proxy-authenticate',
    'connection', 'keep-alive', 'transfer-encoding', 'te', 'trailer',
    'upgrade', 'proxy-connection',
})


class Transport:
    def __init__(self):
        self.cache = {}
        self.page_hops = 0
        self.page_identity = None

    def request(self, url, *, platform, headers=None, method='GET', data=None, expected_id=None):
        seen = set()
        identity = expected_id or self.page_identity
        for hop in range(6):
            url = checked_meta_url(url, platform)
            if url in seen:
                raise ApiError('unavailable')
            seen.add(url)
            if platform == 'facebook' and urlsplit(url).hostname in SOURCE_HOSTS:
                try:
                    current_id = facebook_identity(url)
                except ApiError:
                    # yt-dlp's public tahoe endpoint is the only extra page endpoint.
                    match = re.fullmatch(r'/video/tahoe/async/([0-9]+)/', urlsplit(url).path)
                    if not match or match[1] != identity:
                        raise ApiError('blocked') from None
                    current_id = match[1]
                if identity and current_id and identity != current_id:
                    raise ApiError('unavailable')
                identity = identity or current_id
                self.page_identity = identity
            parts = urlsplit(url)
            connection = PublicHTTPSConnection(parts.hostname, timeout=20, context=ssl.create_default_context())
            try:
                request_headers = dict(headers or PAGE_HEADERS)
                request_headers['Accept-Encoding'] = 'identity'
                connection.request(method, urlunsplit(('', '', parts.path, parts.query, '')),
                                   body=data, headers=request_headers)
                response = connection.getresponse()
                response_headers = dict(response.getheaders())
                if response.status in (301, 302, 303, 307, 308):
                    location = response.getheader('Location')
                    if not location or hop == 5:
                        raise ApiError('unavailable')
                    target = urljoin(url, location)
                    if urlsplit(target).scheme != 'https':
                        raise ApiError('blocked')
                    self.page_hops += 1
                    if self.page_hops > 5:
                        raise ApiError('unavailable')
                    url = target
                    if response.status == 303 or (response.status in (301, 302) and method == 'POST'):
                        method, data = 'GET', None
                    continue
                if response.status in (401, 403, 429):
                    raise ApiError('blocked')
                if response.status not in (200, 206):
                    raise ApiError('unavailable')
                encoding = response.getheader('Content-Encoding', 'identity').lower()
                if encoding not in ('', 'identity'):
                    raise ApiError('unavailable')
                media_type = response.getheader('Content-Type', '').split(';')[0].strip().lower()
                if (media_type.startswith(('audio/', 'video/', 'image/'))
                        or media_type == 'application/octet-stream'):
                    raise ApiError('unsupportedMedia')
                size = response.getheader('Content-Length')
                if size and (not size.isdigit() or int(size) > MAX_META_PAGE):
                    raise ApiError('unavailable')
                body = response.read(MAX_META_PAGE + 1)
                if len(body) > MAX_META_PAGE:
                    raise ApiError('unavailable')
                return url, body, response_headers
            finally:
                connection.close()
        raise ApiError('unavailable')


class CanonicalParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.urls = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'link' and 'canonical' in attrs.get('rel', '').lower().split():
            self.urls.append(attrs.get('href', ''))
        if tag == 'meta' and attrs.get('property', '').lower() == 'og:url':
            self.urls.append(attrs.get('content', ''))


def resolve_page(url, transport):
    expected = facebook_identity(url)
    seen = set()
    for _ in range(6):
        if url in seen:
            raise ApiError('unavailable')
        seen.add(url)
        final, body, headers = transport.request(url, platform='facebook', expected_id=expected)
        expected = expected or transport.page_identity
        final_id = facebook_identity(final)
        if expected and final_id and expected != final_id:
            raise ApiError('unavailable')
        expected = expected or final_id
        text = body.decode('utf-8', errors='replace')
        parser = CanonicalParser()
        parser.feed(text)
        next_url = None
        for candidate in parser.urls:
            candidate = checked_meta_url(urljoin(final, candidate), 'facebook')
            candidate_id = facebook_identity(candidate)
            if candidate_id or next_url is None:
                next_url = candidate
            if candidate_id:
                if expected and expected != candidate_id:
                    raise ApiError('unavailable')
                expected = candidate_id
        if expected:
            # Normalize reels/share aliases for the single, explicitly installed extractor.
            source = final if final_id else next_url
            if not source or facebook_identity(source) is None:
                source = f'https://www.facebook.com/watch/?v={expected}'
            extract_url = f'https://www.facebook.com/video/video.php?v={expected}'
            transport.cache[extract_url] = (source, body, headers)
            return source, expected, extract_url
        if not next_url:
            raise ApiError('unavailable')
        transport.page_hops += 1
        if transport.page_hops > 5:
            raise ApiError('unavailable')
        url = next_url
    raise ApiError('unavailable')


class QuietLogger:
    def debug(self, message):
        pass

    def warning(self, message):
        pass

    def error(self, message):
        pass


class GuardedYoutubeDL(YoutubeDL):
    def __init__(self, transport, platform, video_id):
        self.transport = transport
        self.platform = platform
        self.video_id = video_id
        super().__init__({'quiet': True, 'no_warnings': True, 'logger': QuietLogger(),
                          'noplaylist': True, 'skip_download': True, 'cachedir': False,
                          'check_formats': False, 'proxy': '', 'socket_timeout': 20,
                          'retries': 0, 'extractor_retries': 0, 'http_headers': PAGE_HEADERS,
                          'usenetrc': False, 'cookiefile': None, 'cookiesfrombrowser': None,
                          'js_runtimes': {'quickjs': {'path': '/usr/local/bin/qjs'}},
                          'remote_components': []}, auto_init=False)
        if platform == 'facebook':
            self.add_info_extractor(FacebookIE())
        elif platform == 'youtube':
            self.add_info_extractor(BaseYoutubeIE())
        else:
            raise ApiError('invalidUrl')

    def urlopen(self, req):
        if isinstance(req, str):
            req = Request(req)
        if req.url in self.transport.cache and req.method == 'GET':
            url, body, headers = self.transport.cache[req.url]
        else:
            source_headers = req.headers or {}
            headers = {name: value for name, value in source_headers.items()
                       if name.lower() not in STRIP_HEADERS}
            headers.setdefault('User-Agent', PAGE_HEADERS['User-Agent'])
            headers.setdefault('Accept', '*/*')
            content_type = next((value for name, value in source_headers.items()
                                 if name.lower() == 'content-type'), None)
            if req.data is not None:
                normalized = (content_type or '').split(';')[0].strip().lower()
                if normalized in ('application/json', 'application/x-www-form-urlencoded'):
                    headers['Content-Type'] = content_type
                else:
                    headers['Content-Type'] = 'application/x-www-form-urlencoded'
            try:
                url, body, headers = self.transport.request(
                    req.url, platform=self.platform, headers=headers,
                    method=req.method, data=req.data, expected_id=self.video_id)
            except ApiError as exc:
                raise DownloadError(f'Guarded transport refused metadata request (403 {exc.code})') from None
        return Response(io.BytesIO(body), url, headers)


class FacebookIE(BaseFacebookIE):
    def _parse_json(self, *args, **kwargs):
        value = super()._parse_json(*args, **kwargs)
        # Upstream legacy VideoConfig accepts unrelated IDs; remove them before extraction.
        stack = [value]
        while stack:
            item = stack.pop()
            if isinstance(item, dict):
                for progressive in item.get('progressive_urls', []) or []:
                    if not isinstance(progressive, dict):
                        continue
                    metadata = progressive.get('metadata')
                    if not isinstance(metadata, dict):
                        metadata = progressive['metadata'] = {}
                    quality = str(metadata.get('quality', '')).lower()
                    resolution = re.search(r'[0-9]+', quality)
                    metadata['quality'] = 'hd' if quality == 'hd' or (resolution and int(resolution[0]) >= 720) else 'sd'
                stack.extend(item.values())
            elif isinstance(item, list):
                item[:] = [entry for entry in item if not (
                    isinstance(entry, list) and len(entry) > 2
                    and isinstance(entry[1], list) and entry[1][:1] == ['VideoConfig']
                    and isinstance(entry[2], list) and entry[2] and isinstance(entry[2][0], dict)
                    and str(entry[2][0].get('video_id')) != self._downloader.video_id)]
                stack.extend(item)
        return value


def base_codec(name):
    return (name or '').split('.')[0].lower() if isinstance(name, str) else ''


def positive_int(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def validated_parts(fmt, platform):
    fragments = fmt.get('fragments')
    if not fragments:
        url = fmt.get('url')
        if not isinstance(url, str):
            return None
        return [checked_media_url(url, platform)]
    base = fmt.get('fragment_base_url') or fmt.get('url')
    parts = []
    for fragment in fragments:
        if not isinstance(fragment, dict):
            return None
        if isinstance(fragment.get('url'), str):
            parts.append(checked_media_url(fragment['url'], platform))
        elif isinstance(fragment.get('path'), str) and isinstance(base, str):
            parts.append(checked_media_url(urljoin(base, fragment['path']), platform))
        else:
            return None
        if len(parts) > MAX_PARTS:
            raise ApiError('unsupportedMedia')
    return parts


def describe_format(fmt, platform):
    if not isinstance(fmt, dict) or fmt.get('has_drm'):
        return None
    url = fmt.get('url')
    if not isinstance(url, str):
        return None
    if not fmt.get('fragments') and re.search(r'\.(?:mpd|m3u8)(?:$|[?#])', url, re.I):
        return None
    format_id = str(fmt.get('format_id', '')).lower()
    if 'hls' in format_id or fmt.get('protocol') not in ('https', 'http', None):
        return None
    vcodec, acodec = base_codec(fmt.get('vcodec')), base_codec(fmt.get('acodec'))
    if fmt.get('protocol') not in ('https', 'http') or fmt.get('protocol') is None:
        pass
    if fmt.get('ext') not in (None, 'mp4', 'm4a'):
        return None
    is_muxed = vcodec not in ('', 'none') and acodec not in ('', 'none')
    is_video = vcodec not in ('', 'none') and acodec in ('', 'none')
    is_audio = acodec not in ('', 'none') and vcodec in ('', 'none')
    if not (is_muxed or is_video or is_audio):
        if platform == 'facebook' and (format_id in ('hd', 'sd') or format_id.startswith('progressive_')):
            is_muxed = True  # Legacy progressive path without reliable codec labels.
        else:
            return None
    if platform == 'facebook' and (format_id in ('hd', 'sd') or format_id.startswith('progressive_')):
        pass  # Legacy progressive path without reliable codec labels.
    elif vcodec not in ('', 'none') and vcodec not in ('avc1', 'h264'):
        return None
    elif acodec not in ('', 'none') and acodec not in ('mp4a',):
        return None
    try:
        parts = validated_parts(fmt, platform)
    except ApiError:
        return None
    if not parts:
        return None
    for part in parts:
        if len(part) > MAX_PART_URL:
            return None
    height = fmt.get('height') if isinstance(fmt.get('height'), int) and fmt['height'] > 0 else None
    fps = fmt.get('fps') if isinstance(fmt.get('fps'), (int, float)) and fmt['fps'] > 0 else None
    bitrate = next((value for value in (fmt.get('tbr'), fmt.get('vbr'), fmt.get('abr'))
                    if isinstance(value, (int, float)) and value > 0), 0)
    approx = positive_int(fmt.get('filesize')) or positive_int(fmt.get('filesize_approx'))
    return {'kind': 'muxed' if is_muxed else 'video' if is_video else 'audio',
            'parts': parts, 'height': height, 'fps': fps, 'bitrate': bitrate,
            'abr': fmt.get('abr') if isinstance(fmt.get('abr'), (int, float)) and fmt['abr'] > 0 else bitrate,
            'approxBytes': approx, 'legacy': 1 if format_id == 'hd' else 0}


def clean_text(value, limit):
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value[:limit] if value else None


def validated_thumbnail(value, platform):
    if not isinstance(value, str) or len(value) > MAX_PART_URL:
        return None
    try:
        parts, host = split_url(value)
    except ValueError:
        return None
    if parts.scheme != 'https' or parts.port not in (None, 443) or parts.netloc.lower() != host:
        return None
    if platform == 'youtube' and not suffix_match(host, ('ytimg.com',)):
        return None
    if platform == 'facebook' and not suffix_match(host, ('fbcdn.net', 'facebook.com')):
        return None
    if not suffix_match(host, THUMB_SUFFIXES):
        return None
    return urlunsplit(('https', host, parts.path or '/', parts.query, ''))


def result_headers(source):
    headers = {'User-Agent': PAGE_HEADERS['User-Agent'], 'Referer': source}
    if sum(len(name) + len(value) for name, value in headers.items()) > 8 * 1024:
        raise ApiError('unsupportedMedia')
    return headers


def select_streams(info, platform, video_id, source, height=None):
    if height is not None and (isinstance(height, bool) or not isinstance(height, int) or height <= 0):
        raise ApiError('invalidUrl')
    if not isinstance(info, dict):
        raise ApiError('unavailable')
    if info.get('_type') == 'playlist':
        matches = [entry for entry in info.get('entries', [])
                   if isinstance(entry, dict) and str(entry.get('id')) == video_id]
        if len(matches) != 1:
            raise ApiError('unavailable')
        info = matches[0]
    if str(info.get('id')) != video_id or info.get('_type') not in (None, 'video'):
        raise ApiError('unavailable')
    if info.get('has_drm'):
        raise ApiError('unsupportedMedia')
    if info.get('is_live') or info.get('live_status') in ('is_live', 'is_upcoming'):
        raise ApiError('unsupportedMedia')
    described = [entry for entry in (describe_format(fmt, platform) for fmt in info.get('formats') or [])
                if entry is not None]
    has_audio = any(entry['kind'] == 'audio' for entry in described)
    heights = sorted({entry['height'] for entry in described
                      if entry['height'] is not None
                      and (entry['kind'] == 'muxed' or (entry['kind'] == 'video' and has_audio))},
                     reverse=True)
    if height is not None:
        described = [entry for entry in described
                     if entry['kind'] not in ('video', 'muxed')
                     or (entry['height'] is not None and entry['height'] <= height)]
    muxed = sorted([entry for entry in described if entry['kind'] == 'muxed'],
                   key=lambda entry: (entry['height'] or -1, entry['fps'] or -1,
                                       entry['bitrate'], entry['legacy']), reverse=True)
    videos = sorted([entry for entry in described if entry['kind'] == 'video'],
                    key=lambda entry: (entry['height'] or -1, entry['fps'] or -1, entry['bitrate']),
                    reverse=True)
    audios = sorted([entry for entry in described if entry['kind'] == 'audio'],
                    key=lambda entry: entry['abr'], reverse=True)
    streams = None
    if muxed and videos:
        top_muxed, top_video = muxed[0], videos[0]
        if (top_muxed['height'], top_muxed['fps']) == (top_video['height'], top_video['fps']):
            streams = [('muxed', top_muxed)]
    if streams is None:
        if muxed and (not videos or (muxed[0]['height'] or -1) >= (videos[0]['height'] or -1)):
            streams = [('muxed', muxed[0])]
        elif videos and audios:
            streams = [('video', videos[0]), ('audio', audios[0])]
        elif muxed:
            streams = [('muxed', muxed[0])]
    if streams is None:
        raise ApiError('unsupportedMedia')
    headers = result_headers(source)
    total = 0
    rendered = []
    for kind, entry in streams:
        if entry['approxBytes'] is None:
            total = None
        elif total is not None:
            total += entry['approxBytes']
        rendered.append({'kind': kind, 'parts': entry['parts'],
                         'headers': headers, 'approxBytes': entry['approxBytes']})
    duration = info.get('duration')
    duration_ms = int(duration * 1000) if isinstance(duration, (int, float)) and duration >= 0 else 0
    winner_heights = [entry['height'] for _, entry in streams if entry['height'] is not None]
    result = {'platform': platform, 'videoId': video_id, 'sourceUrl': source,
              'title': clean_text(info.get('title'), 512) or f'Video {platform} {video_id}',
              'uploader': clean_text(info.get('uploader'), 256),
              'durationMs': duration_ms,
              'thumbnailUrl': validated_thumbnail(info.get('thumbnail'), platform),
              'height': max(winner_heights) if winner_heights else None,
              'heights': heights,
              'approxBytes': total, 'streams': rendered}
    if len(json.dumps(result, ensure_ascii=False, separators=(',', ':')).encode('utf-8')) > MAX_RESPONSE:
        raise ApiError('unsupportedMedia')
    return result


def extract(text, height=None):
    platform, source, video_id = shared_url(text)
    transport = Transport()
    if platform == 'facebook':
        source, video_id, extract_url = resolve_page(source, transport)
        ie_key = 'Facebook'
    else:
        extract_url = source
        ie_key = 'Youtube'
    with GuardedYoutubeDL(transport, platform, video_id) as ydl:
        try:
            info = ydl.extract_info(extract_url, download=False, ie_key=ie_key)
        except ApiError:
            raise
        except (OSError, http.client.HTTPException):
            raise ApiError('network') from None
        except (DownloadError, HTTPError) as exc:
            message = str(exc).lower()
            if any(word in message for word in ('login', 'log in', '403', '429', 'checkpoint', 'captcha')):
                raise ApiError('blocked') from None
            raise ApiError('unavailable') from None
    return select_streams(info, platform, video_id, source, height)


def terminate_group(pid):
    if os.name != 'posix' or not pid:
        return
    try:
        os.killpg(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        return
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        try:
            os.killpg(pid, 0)
        except (ProcessLookupError, OSError):
            return
        time.sleep(0.05)
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def extraction_worker(pipe, text, height=None):
    if os.name == 'posix':
        try:
            os.setpgrp()
        except OSError:
            pass
    # Libraries must not print signed URLs, even when an unexpected exception occurs.
    with open(os.devnull, 'w') as sink:
        import sys
        sys.stdout = sys.stderr = sink
        try:
            result = extract(text, height)
        except ApiError as exc:
            result = error_result(exc.code)
        except (OSError, http.client.HTTPException):
            result = error_result('network')
        except (DownloadError, HTTPError) as exc:
            message = str(exc).lower()
            code = 'blocked' if any(word in message for word in ('login', 'log in', '403', '429', 'checkpoint', 'captcha')) else 'unavailable'
            result = error_result(code)
        except Exception:
            result = error_result('unavailable')
        try:
            pipe.send(result)
        finally:
            pipe.close()


def run_extraction(text, height=None):
    context = multiprocessing.get_context('spawn')
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(target=extraction_worker, args=(sender, text, height), daemon=True)
    started = time.monotonic()
    try:
        process.start()
        sender.close()
        # Leave time to reap the worker before the 45-second API budget expires.
        if receiver.poll(max(0, DEADLINE - 0.5 - (time.monotonic() - started))):
            try:
                return receiver.recv()
            except EOFError:
                return error_result('unavailable')
        return error_result('network')
    finally:
        if process.pid:
            if process.is_alive():
                process.terminate()
                process.join(timeout=2)
            terminate_group(process.pid)
            if process.is_alive():
                process.kill()
                process.join()
            elif process.exitcode is None:
                process.join()
        sender.close()
        receiver.close()


class ApiServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, token):
        self.token_digest = hashlib.sha256(token.encode('ascii')).digest()
        self.extractions = threading.BoundedSemaphore(1)
        self.connections = threading.BoundedSemaphore(16)
        super().__init__(address, Handler)

    def process_request(self, request, client_address):
        if not self.connections.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.connections.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.connections.release()

    def handle_error(self, request, client_address):
        pass


class Handler(BaseHTTPRequestHandler):
    server_version = 'NontonYukAPI'
    sys_version = ''

    def setup(self):
        super().setup()
        self.connection.settimeout(20)

    def log_message(self, format, *args):
        pass

    def send_json(self, status, result):
        body = json.dumps(result, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Connection', 'close')
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def do_POST(self):
        auth = self.headers.get_all('Authorization', [])
        supplied = auth[0][7:] if len(auth) == 1 and auth[0].startswith('Bearer ') else ''
        digest = hashlib.sha256(supplied.encode('utf-8')).digest()
        if not hmac.compare_digest(digest, self.server.token_digest):
            self.send_json(401, error_result('unauthorized'))
            return
        if self.path != '/v1/extract':
            self.send_json(404, error_result('invalidUrl'))
            return
        lengths = self.headers.get_all('Content-Length', [])
        content_types = self.headers.get_all('Content-Type', [])
        if (self.headers.get('Transfer-Encoding') is not None or len(lengths) != 1
                or not re.fullmatch(r'[0-9]+', lengths[0])
                or len(content_types) != 1
                or content_types[0].split(';')[0].strip().lower() != 'application/json'):
            self.send_json(400, error_result('invalidUrl'))
            return
        if len(lengths[0]) > 5:
            self.send_json(413, error_result('invalidUrl'))
            return
        size = int(lengths[0])
        if size < 1 or size > MAX_BODY:
            self.send_json(413, error_result('invalidUrl'))
            return
        try:
            raw = self.rfile.read(size)
            if len(raw) != size:
                raise ValueError
            body = json.loads(raw)
            if not isinstance(body, dict) or set(body) not in ({'text'}, {'text', 'height'}):
                raise ValueError
            shared_url(body['text'])
            height = body.get('height')
            if 'height' in body and (isinstance(height, bool) or not isinstance(height, int) or height <= 0):
                raise ValueError
        except (ValueError, UnicodeError, RecursionError, ApiError, OSError):
            self.send_json(400, error_result('invalidUrl'))
            return
        if not self.server.extractions.acquire(blocking=False):
            self.send_json(503, error_result('busy'))
            return
        try:
            result = run_extraction(body['text']) if 'height' not in body else run_extraction(body['text'], body['height'])
        except Exception:
            result = error_result('unavailable')
        finally:
            self.server.extractions.release()
        code = result.get('error', {}).get('code')
        status = {'invalidUrl': 400, 'unavailable': 422, 'blocked': 422,
                  'unsupportedMedia': 422, 'network': 502}.get(code, 200)
        self.send_json(status, result)

    def do_GET(self):
        if self.path == '/healthz':
            self.send_json(200, {'status': 'ok'})
            return
        self.send_json(405, error_result('invalidUrl'))


def main():
    token = os.environ.get('NONTONIN_API_TOKEN', '')
    if len(token) < 32 or not token.isascii() or any(char.isspace() for char in token):
        raise SystemExit('Set NONTONIN_API_TOKEN to a random ASCII token of at least 32 characters.')
    with ApiServer(('0.0.0.0', 8080), token) as server:
        server.serve_forever()


if __name__ == '__main__':
    main()
