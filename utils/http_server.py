import json
import multiprocessing
import os
import re
import socket
import subprocess
import time
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler
from http.server import HTTPServer
from socketserver import ThreadingMixIn

from utils.data_parser import parse_received_data_emby, parse_received_data_plex, list_episodes, \
    parse_received_data_fntv, list_episodes_fntv
from utils.downloader import DownloadManager
from utils.net_tools import update_server_playback_progress, sync_third_party_for_eps
from utils.player_manager import PlayerManager
from utils.players import start_player_func_dict, stop_sec_func_dict
from utils.tools import (configs, MyLogger, open_local_folder, play_media_file,
                         activate_window_by_pid, get_player_cmd, ThreadWithReturnValue,
                         create_sparse_file)
from utils.trakt_sync import trakt_api_client

player_is_running = False
logger = MyLogger()
dl_manager = DownloadManager(configs.cache_path, speed_limit=configs.speed_limit)
miss_runtime_start_sec = {}


def get_machine_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('223.5.5.5', 80))
        local_ip = s.getsockname()[0]
        s.close()
    except Exception:
        local_ip = '127.0.0.1'
    return local_ip


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    """Handle requests in a separate thread."""


def run_server(ip='127.0.0.1', port=58000):
    if not configs.raw.getboolean('dev', 'listen_on_localhost', fallback=True):
        ip = get_machine_ip()
    server_address = (ip, port)
    httpd = ThreadingHTTPServer(server_address, UserScriptRequestHandler)
    logger.info('serving at http://%s:%d' % server_address)
    httpd.serve_forever()


class UserScriptRequestHandler(BaseHTTPRequestHandler):

    def _post_resopne(self, msg=None, status=200):
        self.send_response(status)
        self.send_header('Content-type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        msg = msg or {'msg': 'default'}
        self.wfile.write(json.dumps(msg).encode('utf-8'))

    def do_POST(self):
        length = int(self.headers.get('content-length'))
        data = json.loads(self.rfile.read(length))
        configs.update()
        if 'ToLocalPlayer' in self.path:
            self._post_resopne()
            if data.get('showTaskManager'):
                from utils.gui import show_task_manager
                # multiprocessing.Process(target=show_task_manager, daemon=True).start()
                # 多进程会复制 dl_manager 导致如果正在下载的话，会重复启动下载任务。
                threading.Thread(target=show_task_manager, daemon=True).start()
                # tkinter 不是线程安全的，可能会导致退出。
                return True
            data = parse_received_data_emby(data) if self.path.startswith('/emby') else parse_received_data_plex(data)
            logger.info(f"server={data['server']}/{data.get('server_version')} {data['mount_disk_mode']=}")
            if configs.check_str_match(_str=data['netloc'], section='gui', option='except_host'):
                threading.Thread(target=start_play, args=(data,), daemon=True).start()
                return True
        if 'fnvideo' in self.path:
            self._post_resopne()
            data = parse_received_data_fntv(data)
            logger.info(f"server=fntv {data.get('type')} {data.get('item_guid')}")
            threading.Thread(target=start_play_fntv, args=(data,), daemon=True).start()
            return True
        thread_dict = {
            'play': threading.Thread(target=start_play, args=(data,)),
            'play_check': threading.Thread(target=dl_manager.play_check, args=(data,)),
            'download_play': threading.Thread(target=dl_manager.download_play, args=(data,)),
            'download_not_play': threading.Thread(target=dl_manager.download_play, args=(data, False)),
            'download_only': threading.Thread(target=dl_manager.download_only, args=(data,)),
            'delete_by_id': threading.Thread(target=dl_manager.delete, args=({}, data.get('_id'))),
            'delete': threading.Thread(target=dl_manager.delete, args=(data,)),
            'resume_or_pause': threading.Thread(target=dl_manager.resume_or_pause, args=(data,)),
        }
        [setattr(t, 'daemon', True) for t in thread_dict.values()]

        if self.path.startswith('/action'):
            if self.path.endswith('sparse_file'):
                cache_dir = configs.raw.get('gui', 'server_cache_path', fallback='')
                if not cache_dir:
                    logger.error('gui[server_cache_path] missing, check it')
                    return
                create_sparse_file(os.path.join(cache_dir, data['name']), data['size'])
                return self._post_resopne({'sparse_file': True})

        self._post_resopne()
        if self.path in ('/gui', '/dl', '/pl'):
            gui_cmd = data['gui_cmd']
            logger.info(self.path, gui_cmd)
            thread_dict[gui_cmd].start()
        elif 'ToLocalPlayer' in self.path:
            if configs.gui_is_enable:
                if configs.raw.get('gui', 'enable_path'):
                    if not configs.check_str_match(data['file_path'], 'gui', 'enable_path', log_by=False):
                        thread_dict['play'].start()
                        return True
                if configs.raw.getboolean('gui', 'without_confirm', fallback=False):
                    thread_dict['download_play'].start()
                    return True
                from utils.gui import show_ask_button
                logger.info('show ask button')
                if configs.platform != 'Darwin':
                    threading.Thread(target=show_ask_button, args=(data,), daemon=True).start()
                else:
                    multiprocessing.Process(target=show_ask_button, args=(data,), daemon=True).start()
            else:
                thread_dict['play'].start()
        elif 'openFolder' in self.path:
            open_local_folder(data)
        elif 'playMediaFile' in self.path:
            play_media_file(data)
        else:
            logger.error(self.path, ' not allow')
            self._post_resopne({'msg': f'{self.path} not allow'})

    def do_OPTIONS(self):
        self._post_resopne()

    def do_GET(self):
        if self.path in ['/', '/favicon.ico']:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'Server is running')
            return
        if self.path.startswith('/send_media_file'):
            self.send_media_file()
            return
        if self.path.startswith('/trakt_auth'):
            parsed_path, query = self.parse_get_query()
            if received_code := query.get('code'):
                trakt_api_client(received_code)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'etlp: trakt auth success')
            logger.info(f'trakt: auth success')
            return
        if self.path.startswith('/miss_runtime_start_sec'):
            self.check_miss_runtime_start_sec()
            return
        logger.info(f'path invalid {self.path=}')

    def return_json(self, data):
        self.wfile.write(json.dumps(data).encode('utf8'))

    def parse_get_query(self):
        parsed_path = urllib.parse.urlparse(self.path)
        query = dict(urllib.parse.parse_qsl(parsed_path.query))
        return parsed_path, query

    def check_miss_runtime_start_sec(self):
        parsed_path, query = self.parse_get_query()
        stop_sec = query.get('stop_sec')
        netloc, item_id, basename = query.get('netloc'), query.get('item_id'), query.get('basename')
        key = f'{netloc}-{item_id}'
        self.send_response(200)
        self.end_headers()
        if stop_sec:
            miss_runtime_start_sec[key] = int(float(stop_sec))
            return
        start_sec = miss_runtime_start_sec.get(key, 0)
        self.return_json({'start_sec': start_sec})

    def send_media_file(self):
        parsed_path, query = self.parse_get_query()
        req_token = query.get('token', '')
        server_token = configs.raw.get('dev', 'http_server_token', fallback='')
        if req_token != server_token:
            logger.info(f'req_token invalid: {req_token=} {server_token=}')
            return

        video_path = urllib.parse.unquote(query['file_path'])

        video_ext = ['webm', 'mkv', 'flv', 'vob', 'ogv', 'ogg', 'rrc', 'gifv', 'mng', 'mov', 'avi', 'qt', 'wmv', 'yuv',
                     'rm', 'asf', 'amv', 'mp4', 'm4p', 'm4v', 'mpg', 'mp2', 'mpeg', 'mpe', 'mpv', 'm4v', 'svi', '3gp',
                     '3g2', 'mxf', 'roq', 'nsv', 'flv', 'f4v', 'f4p', 'f4a', 'f4b', 'mod']
        sub_ext = ['srt', 'sub', 'ass', 'ssa', 'vtt', 'sbv', 'smi', 'sami', 'mpl', 'txt', 'dks', 'pjs', 'stl', 'usf',
                   'cdg', 'idx', 'ttml']
        valid_ext = tuple(video_ext + sub_ext)

        if not video_path.endswith(valid_ext):
            logger.info(f'ext invalid: {video_path}')
            return

        if not os.path.exists(video_path):
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b'File not found')
            return

        file_size = os.path.getsize(video_path)
        chunk_size = 8 * 1024 * 1024
        range_header = self.headers.get('Range', None)

        if range_header:
            start, end = self.parse_range_header(range_header, file_size)
            logger.info(f'range={start}-{end} | {video_path}')
            if start >= file_size or end >= file_size or start > end:
                self.send_response(416)
                self.send_header('Content-Range', f'bytes */{file_size}')
                self.end_headers()
                return

            self.send_response(206)
            self.send_header('Content-type', 'octet-stream')
            self.send_header('Content-Range', f'bytes {start}-{end}/{file_size}')
            self.send_header('Content-Length', str(end - start + 1))
            self.end_headers()

            with open(video_path, 'rb') as file:
                file.seek(start)
                bytes_to_read = end - start + 1
                while bytes_to_read > 0:
                    chunk = file.read(min(chunk_size, bytes_to_read))
                    if not chunk:
                        break
                    try:
                        self.wfile.write(chunk)
                    except ConnectionError:
                        break
                    bytes_to_read -= len(chunk)

        else:
            logger.info(f'range: 0- | {video_path}')
            self.send_response(200)
            self.send_header('Content-type', 'octet-stream')
            self.send_header('Content-Length', str(file_size))
            self.end_headers()

            with open(video_path, 'rb') as file:
                while chunk := file.read(chunk_size):
                    try:
                        self.wfile.write(chunk)
                    except ConnectionError:
                        break

    @staticmethod
    def parse_range_header(range_header, file_size):
        match = re.match(r'bytes=(\d*)-(\d*)', range_header)
        if match:
            start = match.group(1)
            end = match.group(2)
            start = int(start) if start else 0
            end = int(end) if end else file_size - 1
            return start, end
        return 0, file_size - 1


def _fntv_subtitle_download(api, subtitle_guid, tmp_dir):
    """下载飞牛字幕到本地文件, 返回路径"""
    if not subtitle_guid:
        return None
    sub_content = api.download_subtitle(subtitle_guid)
    if not sub_content:
        return None
    ext = '.srt' if sub_content.startswith(('1', '0', '[', '<')) else '.ass'
    os.makedirs(tmp_dir, exist_ok=True)
    sub_local = os.path.join(tmp_dir, f'fntv_sub_{subtitle_guid}{ext}')
    with open(sub_local, 'w', encoding='utf-8') as f:
        f.write(sub_content)
    logger.info(f'fntv: subtitle -> {sub_local}')
    return sub_local


def start_play_fntv(data):
    """飞牛影视: 获取播放链接并启动本地播放器"""
    global player_is_running
    from utils.fntv_api import FntvApi

    api = FntvApi(
        host=data['host'],
        token=data['token'],
        http_proxy=configs.script_proxy,
    )

    media_guid = data['media_guid']
    video_guid = data['video_guid']
    audio_guid = data['audio_guid']
    subtitle_guid = data.get('subtitle_guid', '')
    start_sec = data.get('start_sec', 0)
    total_sec = data.get('total_sec', 0)
    media_title = data.get('media_title', data.get('file_name', ''))
    resolution = data.get('resolution', '1080p')
    bitrate = data.get('bitrate', 0)
    codec_name = data.get('codec_name', 'hevc')
    host = data['host']
    token = data['token']

    logger.info(f'fntv: start play {media_title}, start_sec={start_sec}, total_sec={total_sec}')

    # Step 1: 获取播放链接 (m3u8)
    play_data = api.start_play(
        media_guid=media_guid,
        video_guid=video_guid,
        audio_guid=audio_guid,
        subtitle_guid=subtitle_guid,
        video_encoder=codec_name,
        resolution=resolution,
        bitrate=bitrate,
        start_timestamp=int(start_sec),
    )

    play_link = play_data.get('play_link', '')
    if not play_link:
        logger.error('fntv: failed to get play link')
        return

    stream_url = FntvApi.build_play_url(host, play_link)
    data['stream_url'] = stream_url
    data['media_path'] = stream_url

    # Step 2: 下载外挂字幕到本地（飞牛API需要认证，mpv无法直接访问）
    sub_file = data.get('sub_file')
    if sub_file and sub_file.startswith('http'):
        try:
            import tempfile
            subtitle_guid_dl = data.get('subtitle_guid', '')
            if subtitle_guid_dl:
                sub_content = api.download_subtitle(subtitle_guid_dl)
                if sub_content:
                    # 保存到临时文件
                    ext = '.srt'
                    for s in data.get('subtitle_streams', []):
                        if s['guid'] == subtitle_guid_dl:
                            fmt = s.get('format', 'srt')
                            ext = f'.{fmt}' if fmt else '.srt'
                            break
                    tmp_dir = os.path.join(configs.cwd, '.tmp')
                    os.makedirs(tmp_dir, exist_ok=True)
                    sub_local = os.path.join(tmp_dir, f'fntv_sub_{subtitle_guid_dl}{ext}')
                    with open(sub_local, 'w', encoding='utf-8') as f:
                        f.write(sub_content)
                    sub_file = sub_local
                    logger.info(f'fntv: subtitle downloaded to {sub_local}')
        except Exception as e:
            logger.error(f'fntv: subtitle download failed: {e}')
            sub_file = None

    # Step 3: 构建播放器命令
    logger.info(f'fntv: stream_url={stream_url}')
    cmd = get_player_cmd(media_path=stream_url, file_path=data.get('file_path', stream_url), data=data)
    player_path = cmd[0]
    player_path_lower = player_path.lower()

    # 注入 HTTP header 认证（飞牛m3u8及其.ts分段需要认证）
    if 'mpv' in player_path_lower or 'iina' in player_path_lower:
        origin = host if host.startswith('http') else f'https://{host}'
        http_headers = f'Authorization: {token}'
        cmd.insert(1, f'--http-header-fields={http_headers}')
        logger.info('fntv: added http-header-fields for mpv auth')

    logger.info(f'fntv: cmd={" ".join(cmd)}')

    # Step 4: 启动播放器
    legal_player_name = list(start_player_func_dict)
    player_name_list = [i for i in legal_player_name if i in player_path_lower]

    if player_name_list:
        player_name = player_name_list[0]
        player_function = start_player_func_dict[player_name]
        try:
            stop_sec_kwargs = player_function(
                cmd=cmd, start_sec=start_sec, sub_file=sub_file,
                media_title=media_title, mount_disk_mode=False, data=data,
            )
            stop_sec = stop_sec_func_dict[player_name](**stop_sec_kwargs)
        except Exception as e:
            logger.error(f'fntv: player error: {e}')
            player = subprocess.Popen(cmd)
            player.wait()
            stop_sec = None
    else:
        logger.info('fntv: run as generic player')
        player = subprocess.Popen(cmd)
        activate_window_by_pid(player.pid)
        player.wait()
        stop_sec = None

    # Step 4: 回传播放进度
    if stop_sec is not None and total_sec > 0:
        # 如果播放器正常关闭并返回停止时间
        progress = stop_sec / total_sec
        logger.info(f'fntv: stop_sec={stop_sec}, progress={progress:.1%}')
        if progress > 0.01:
            # 只在有实际播放时才回传
            api.record_progress(
                item_guid=data['item_guid'],
                media_guid=media_guid,
                video_guid=video_guid,
                audio_guid=audio_guid,
                subtitle_guid=subtitle_guid,
                resolution=resolution,
                bitrate=bitrate,
                ts=int(stop_sec),
                duration=int(total_sec),
                play_link=play_link,
            )
            logger.info(f'fntv: progress synced: {int(stop_sec)}/{int(total_sec)}s')
    elif stop_sec is None and total_sec > 0:
        # 播放器没有提供进度(比如播放器不支持 IPC, 或被直接关闭)
        logger.info('fntv: no stop_sec from player, skipping progress sync')

    # Step 5: 播放器停止后将状态重置
    player_is_running = False


def start_play_fntv(data):
    """飞牛影视播放入口, mpv 原生播放列表"""
    global player_is_running
    from utils.fntv_api import FntvApi
    from utils.data_parser import list_episodes_fntv

    api = FntvApi(host=data['host'], token=data['token'], http_proxy=configs.script_proxy)
    host = data['host']
    token = data['token']
    item_type = data.get('type', '')
    item_guid = data.get('item_guid', '')
    tmp_dir = os.path.join(configs.cwd, '.tmp')
    os.makedirs(tmp_dir, exist_ok=True)
    ep_meta_cache = {}  # title -> metadata dict

    # ── 1. Ep list ──
    if item_type == 'Movie':
        eps_list, start_idx, first_ep = [data], 0, data
    else:
        eps_list = list_episodes_fntv(data) or [data]
        start_idx = 0
        for i, ep in enumerate(eps_list):
            if ep.get('item_guid', '') == item_guid:
                start_idx = i; break
        first_ep = data  # userscript sent full data for first episode
        logger.info(f'fntv: {len(eps_list)} eps, start={start_idx + 1}')

    # ── 2. First ep: play_link + subtitle ──
    mg = first_ep.get('media_guid', '')
    vg = first_ep.get('video_guid', '')
    ag = first_ep.get('audio_guid', '')
    res = first_ep.get('resolution', '1080p')
    br = first_ep.get('bitrate', 0)
    codec = first_ep.get('codec_name', 'hevc')
    start_sec = first_ep.get('start_sec', 0)
    media_title = first_ep.get('media_title', first_ep.get('title', ''))
    sub_streams = first_ep.get('subtitle_streams', [])

    sguid = ''
    for s in sub_streams:
        if s.get('codec_name') in ('ass', 'subrip') and s.get('is_external', 0):
            sguid = s.get('guid', ''); break
    sub_local = _fntv_subtitle_download(api, sguid, tmp_dir) if sguid else None

    pd = api.start_play(media_guid=mg, video_guid=vg, audio_guid=ag,
                        video_encoder=codec, resolution=res, bitrate=br)
    pl = pd.get('play_link', '')
    if not pl:
        logger.error('fntv: no play_link'); player_is_running = False; return
    first_url = host.rstrip('/') + pl if not pl.startswith('http') else pl

    # Cache first ep metadata for progress sync
    ep_meta_cache[media_title] = dict(
        idx=start_idx, item_guid=item_guid, media_guid=mg,
        video_guid=vg, audio_guid=ag, resolution=res, bitrate=br,
        total_sec=first_ep.get('total_sec', 0),
        episode_number=first_ep.get('episode_number', start_idx + 1))

    # ── 3. Launch mpv (NO --sub-file on cmdline — managed via IPC) ──
    cmd = get_player_cmd(media_path=first_url, file_path=first_url, data=data)
    pp = cmd[0]
    if 'mpv' in pp.lower():
        cmd.insert(1, f'--http-header-fields=Authorization: {token}')
    logger.info(f'fntv: mpv ep {start_idx + 1}')

    pnl = [i for i in list(start_player_func_dict) if i in pp.lower()]
    if not pnl:
        logger.error('fntv: mpv not found'); player_is_running = False; return

    pn = pnl[0]
    try:
        skw = start_player_func_dict[pn](cmd=cmd, start_sec=start_sec, sub_file=None,
                                           media_title=media_title, mount_disk_mode=False, data=data)
    except Exception as e:
        logger.error(f'fntv: start error: {e}'); player_is_running = False; return

    mpv = skw.get('mpv')
    t0 = time.time()

    # Manage subtitles via IPC: all external subtitles loaded via sub-add/sub-remove
    ep_sub_map = {}  # media_title -> local subtitle path

    if mpv and sub_local:
        ep_sub_map[media_title] = sub_local

    if mpv:
        # Wait a bit, then add first ep subtitle
        if sub_local:
            import time as _t
            _t.sleep(0.8)
            try:
                mpv.command('sub-add', sub_local)
                logger.info('fntv: sub added for ep 1')
            except Exception as e:
                logger.error(f'fntv: sub-add failed: {e}')

        # file-loaded handler: swap subtitles when mpv changes episode
        @mpv.on_event('file-loaded')
        def on_file_loaded(_):
            current_title = None
            try:
                current_title = mpv.command('get_property', 'media-title')
                track_list = mpv.command('get_property', 'track-list') or []
                for t in track_list:
                    if t.get('type') == 'sub' and t.get('external'):
                        mpv.command('sub-remove', t['id'])
            except Exception:
                pass
            sub_path = ep_sub_map.get(current_title) if current_title else None
            if sub_path:
                try:
                    mpv.command('sub-add', sub_path)
                except Exception as e:
                    logger.error(f'fntv: sub-add error: {e}')

    # ── 4. Background: add remaining eps to mpv playlist ──
    limit = configs.raw.getint('playlist', 'item_limit', fallback=-1)
    if limit < 0: limit = len(eps_list)
    remaining = [(i, eps_list[i]) for i in range(start_idx + 1, start_idx + limit) if i < len(eps_list)]

    if mpv and remaining and not getattr(mpv, 'is_iina', False):
        new_loadfile = False
        try:
            for c in mpv.command('get_property', 'command-list'):
                if c['name'] == 'loadfile':
                    for a in c['args']:
                        if a['name'] == 'index': new_loadfile = True
        except Exception: pass

        if not new_loadfile:
            logger.info('fntv: old mpv, playlist disabled')
        else:
            import concurrent.futures

            def fetch_and_add(idx, ep):
                guid = ep.get('item_guid', '')
                if not guid: return
                sd = api.get_stream_list(guid) or {}
                fs = sd.get('files', [])
                if not fs: return
                vs = sd.get('video_streams', []); als = sd.get('audio_streams', [])
                ss = sd.get('subtitle_streams', [])
                fi = fs[0]; vi = vs[0] if vs else {}; ai = als[0] if als else {}
                emg = fi['guid']; evg = vi.get('guid', ''); eag = ai.get('guid', '')
                eres = vi.get('resolution_type', '1080p'); ebr = vi.get('bps', 0)
                ecodec = vi.get('codec_name', 'hevc')

                epd = api.start_play(media_guid=emg, video_guid=evg, audio_guid=eag,
                                     video_encoder=ecodec, resolution=eres, bitrate=ebr)
                epl = epd.get('play_link', '')
                if not epl: return
                esurl = host.rstrip('/') + epl if not epl.startswith('http') else epl

                esguid = ''
                for s in ss:
                    if s.get('codec_name') in ('ass', 'subrip') and s.get('is_external', 0):
                        esguid = s.get('guid', ''); break
                esub = _fntv_subtitle_download(api, esguid, tmp_dir) if esguid else None

                etitle = ep.get('media_title', ep.get('title', ''))
                opts = f'force-media-title="{etitle}",osd-playing-msg="{etitle}",start=0'
                if esub:
                    ep_sub_map[etitle] = esub
                try:
                    mpv.command('loadfile', esurl, 'append', '-1', opts)
                    logger.info(f'fntv: + ep {ep.get("episode_number", idx)}')
                    ep_meta_cache[etitle] = dict(
                        idx=idx, item_guid=guid, media_guid=emg, video_guid=evg,
                        audio_guid=eag, resolution=eres, bitrate=ebr,
                        total_sec=ep.get('total_sec', vi.get('duration', 0)),
                        episode_number=ep.get('episode_number', 0))
                except Exception as e:
                    logger.error(f'fntv: add ep failed: {e}')

            if len(remaining) <= 6:
                for idx, ep in remaining: fetch_and_add(idx, ep)
            else:
                with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
                    futures = [ex.submit(fetch_and_add, idx, ep) for idx, ep in remaining]
                    concurrent.futures.wait(futures)

    logger.info(f'fntv: playlist loaded in {time.time() - t0:.1f}s')

    # ── 5. Wait for mpv exit ──
    playlist_time = {}; playlist_total_sec = {}
    if mpv:
        result = stop_sec_func_dict[pn](**skw)
        if isinstance(result, tuple):
            playlist_time, playlist_total_sec = result
        elif isinstance(result, dict):
            playlist_time = result

    # ── 6. Sync progress ──
    if mpv and isinstance(playlist_time, dict):
        sync_data = {media_title: first_ep}
        for title, meta in ep_meta_cache.items():
            idx = meta.get('idx', 0)
            if idx < len(eps_list):
                eps_list[idx].update({k: v for k, v in meta.items() if k != 'idx'})
            sync_data[title] = eps_list[idx] if idx < len(eps_list) else meta

        for ep_title, stop_sec in playlist_time.items():
            if not stop_sec: continue
            ep = sync_data.get(ep_title)
            if not ep: continue
            ts_sec = playlist_total_sec.get(ep_title, ep.get('total_sec', 0))
            if ts_sec <= 0 or stop_sec / ts_sec < 0.01: continue
            pct = stop_sec / ts_sec
            logger.info(f'fntv: sync ep{ep.get("episode_number","?")} {int(stop_sec)}/{int(ts_sec)}s ({pct:.0%})')
            api.record_progress(
                item_guid=ep.get('item_guid', ''), media_guid=ep.get('media_guid', ''),
                video_guid=ep.get('video_guid', ''), audio_guid=ep.get('audio_guid', ''),
                resolution=ep.get('resolution', ''), bitrate=ep.get('bitrate', 0),
                ts=int(stop_sec), duration=int(ts_sec), play_link='')

    if sub_local:
        try:
            os.remove(sub_local)
        except Exception:
            pass
    player_is_running = False
    logger.info('fntv: done')



def start_play(data):
    global player_is_running
    if player_is_running:
        logger.error('player_is_running, skip. You may want to disable one_instance_mode, see detail in config file')
        return
    file_path = data['file_path']
    start_sec = data['start_sec']
    sub_file = data['sub_file']
    media_title = data['media_title']
    mount_disk_mode = data['mount_disk_mode']
    eps_data_thread = ThreadWithReturnValue(target=list_episodes, args=(data,))
    eps_data_thread.start()

    cmd = get_player_cmd(media_path=data['media_path'], file_path=file_path, data=data)
    player_path = cmd[0]
    player_path_lower = player_path.lower()
    # 播放器特殊处理
    player_is_running = True if configs.raw.getboolean('dev', 'one_instance_mode', fallback=True) else False
    player_alias_dict = {'ddplay': 'dandanplay'}
    legal_player_name = list(start_player_func_dict) + list(player_alias_dict)
    player_name = [i for i in legal_player_name if i in player_path_lower]
    if player_name:
        player_name = player_name[0]
        player_name = player_alias_dict.get(player_name, player_name)
        if configs.check_str_match(_str=data['netloc'], section='playlist', option='enable_host', fallback=True) \
                and player_name in ('mpv', 'vlc', 'mpc', 'potplayer', 'iina') \
                or (player_name == 'dandanplay' and mount_disk_mode):
            player_manager = PlayerManager(data=data, player_name=player_name, player_path=player_path)
            player_manager.start_player(cmd=cmd, start_sec=start_sec, sub_file=sub_file, media_title=media_title,
                                        mount_disk_mode=mount_disk_mode, data=data)
            eps_data = eps_data_thread.join()
            player_manager.playlist_add(eps_data=eps_data)
            player_manager.update_playlist_time_loop()
            player_manager.update_playback_for_eps()
            player_is_running = False
            return

        player_function = start_player_func_dict[player_name]
        stop_sec_kwargs = player_function(cmd=cmd, start_sec=start_sec, sub_file=sub_file, media_title=media_title,
                                          mount_disk_mode=mount_disk_mode, data=data)
        stop_sec = stop_sec_func_dict[player_name](**stop_sec_kwargs)
        logger.info('stop_sec', stop_sec)
        if stop_sec is None:
            player_is_running = False
            return
        total_sec = data['total_sec']
        progress_percent = stop_sec / total_sec
        if total_sec != 86400 or progress_percent > 0.9:
            update_server_playback_progress(stop_sec=stop_sec, data=data)
        if total_sec == 86400:
            logger.info('skip update progress, cuz miss runtime data, may need to enable playlist')
        eps_data = eps_data_thread.join()
        current_ep = [i for i in eps_data if i['file_path'] == data['file_path']][0]
        current_ep['_stop_sec'] = stop_sec
        for provider in 'trakt', 'bangumi':
            if configs.raw.get(provider, 'enable_host', fallback=''):
                threading.Thread(target=sync_third_party_for_eps,
                                 kwargs={'eps': [current_ep], 'provider': provider}, daemon=True).start()

        if configs.gui_is_enable \
                and progress_percent * 100 > configs.raw.getfloat('gui', 'delete_at', fallback=99.9):
            logger.info('watched, delete cache')
            threading.Thread(target=dl_manager.delete, args=(data,), daemon=True).start()
    else:
        logger.info('run as not support player mod')
        logger.info(cmd)
        player = subprocess.Popen(cmd)
        activate_window_by_pid(player.pid)
    player_is_running = False
