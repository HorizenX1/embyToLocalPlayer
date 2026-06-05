"""飞牛影视 API 客户端"""
import urllib.parse

from utils.net_tools import requests_urllib
from utils.configs import MyLogger

logger = MyLogger()


class FntvApi:

    def __init__(self, host, token, http_proxy=''):
        self.host = host.rstrip('/')
        self.token = token
        self.base = f'{self.host}/v/api/v1'
        self.http_proxy = http_proxy

    def _headers(self, content_type='application/json'):
        return {
            'Authorization': self.token,
            'Content-Type': content_type,
            'Accept': 'application/json, text/plain, */*',
            'Origin': self.host,
        }

    def _get(self, path, params=None):
        url = f'{self.base}{path}'
        return requests_urllib(url, headers=self._headers(),
                               params=params, get_json=True,
                               http_proxy=self.http_proxy)

    def _post(self, path, data=None):
        url = f'{self.base}{path}'
        return requests_urllib(url, headers=self._headers(),
                               _json=data, get_json=True,
                               http_proxy=self.http_proxy)

    def get_item(self, guid):
        """获取条目详情 (电影/TV/季/集)"""
        r = self._get(f'/item/{guid}')
        return r.get('data', {}) if r.get('code') == 0 else {}

    def get_stream_list(self, guid):
        """获取媒体流信息 (文件路径、视频/音频/字幕流)"""
        r = self._get(f'/stream/list/{guid}')
        return r.get('data', {}) if r.get('code') == 0 else {}

    def get_play_info(self, item_guid, media_guid=None):
        """获取播放信息 (上次播放位置、默认音视频字幕选择)"""
        body = {'item_guid': item_guid}
        if media_guid:
            body['media_guid'] = media_guid
        r = self._post('/play/info', data=body)
        return r.get('data', {}) if r.get('code') == 0 else {}

    def start_play(self, media_guid, video_guid, audio_guid, subtitle_guid='',
                   video_encoder='hevc', resolution='4k', bitrate=0,
                   start_timestamp=0, audio_encoder='aac', channels=2, forced_sdr=0):
        """获取播放链接 (m3u8 地址)"""
        body = {
            'media_guid': media_guid,
            'video_guid': video_guid,
            'video_encoder': video_encoder,
            'resolution': resolution,
            'bitrate': bitrate,
            'startTimestamp': start_timestamp,
            'audio_encoder': audio_encoder,
            'audio_guid': audio_guid,
            'subtitle_guid': subtitle_guid,
            'channels': channels,
            'forced_sdr': forced_sdr,
        }
        r = self._post('/play/play', data=body)
        return r.get('data', {}) if r.get('code') == 0 else {}

    def record_progress(self, item_guid, media_guid, video_guid, audio_guid,
                        subtitle_guid='', resolution='', bitrate=0,
                        ts=0, duration=0, play_link=''):
        """回传播放进度"""
        body = {
            'item_guid': item_guid,
            'media_guid': media_guid,
            'video_guid': video_guid,
            'audio_guid': audio_guid,
            'subtitle_guid': subtitle_guid,
            'resolution': resolution,
            'bitrate': bitrate,
            'ts': int(ts),
            'duration': int(duration),
            'play_link': play_link,
        }
        r = self._post('/play/record', data=body)
        return r.get('code') == 0

    def get_season_list(self, tv_guid):
        """获取 TV 的所有季"""
        r = self._get(f'/season/list/{tv_guid}')
        return r.get('data', []) if r.get('code') == 0 else []

    def get_episode_list(self, season_guid):
        """获取季的所有剧集"""
        r = self._get(f'/episode/list/{season_guid}')
        return r.get('data', []) if r.get('code') == 0 else []

    def download_subtitle(self, subtitle_guid):
        """下载字幕文本, 返回字幕内容字符串"""
        url = f'{self.base}/subtitle/dl/{subtitle_guid}'
        try:
            resp = requests_urllib(url, headers=self._headers(),
                                   http_proxy=self.http_proxy,
                                   decode=True)
            return resp
        except Exception as e:
            logger.error(f'download subtitle failed: {e}')
            return ''

    @staticmethod
    def parse_stream_data(stream_list_data):
        """从 stream/list 返回数据中提取结构化信息"""
        files = stream_list_data.get('files', [])
        video_streams = stream_list_data.get('video_streams', [])
        audio_streams = stream_list_data.get('audio_streams', [])
        subtitle_streams = stream_list_data.get('subtitle_streams', [])

        file_info = files[0] if files else {}
        video_info = video_streams[0] if video_streams else {}
        audio_info = audio_streams[0] if audio_streams else {}

        return {
            'media_guid': file_info.get('guid', ''),
            'file_path': file_info.get('path', ''),
            'file_name': file_info.get('file_name', ''),
            'file_size': file_info.get('size', 0),
            'video_guid': video_info.get('guid', ''),
            'audio_guid': audio_info.get('guid', ''),
            'resolution': video_info.get('resolution_type', ''),
            'codec_name': video_info.get('codec_name', ''),
            'bitrate': video_info.get('bps', 0),
            'duration': video_info.get('duration', 0),
            'subtitle_streams': subtitle_streams,
            'audio_streams': audio_streams,
            'video_streams': video_streams,
        }

    @staticmethod
    def build_play_url(host, play_link):
        """构建完整播放 URL"""
        if play_link.startswith('http'):
            return play_link
        return f"{host.rstrip('/')}{play_link}"

    @staticmethod
    def build_subtitle_url(host, subtitle_guid):
        """构建完整字幕下载 URL"""
        return f"{host.rstrip('/')}/v/api/v1/subtitle/dl/{subtitle_guid}"
