# 飞牛影视适配方案

## 一、分析结果汇总

### 1.1 API 端点对照表

| Emby/Jellyfin API | 飞牛影视 API | 请求方式 |
|---|---|---|
| `Items/{id}/PlaybackInfo` | `/v/api/v1/play/info` | POST `{"item_guid":"..."}` |
| `Items/{id}/PlaybackInfo` (MediaSources) | `/v/api/v1/stream/list/{guid}` | GET |
| - (获取播放地址) | `/v/api/v1/play/play` | POST (含 media/video/audio/subtitle guid) |
| `Sessions/Playing/Progress` | `/v/api/v1/play/record` | POST `{"item_guid":"...","ts":<秒>,"duration":<秒>}` |
| `Users/{uid}/Items/{id}` | `/v/api/v1/item/{guid}` | GET |
| `Shows/{id}/Seasons` | `/v/api/v1/season/list/{tv_guid}` | GET |
| `Seasons/{id}/Episodes` | `/v/api/v1/episode/list/{season_guid}` | GET |
| 字幕下载 | `/v/api/v1/subtitle/dl/{subtitle_guid}` | GET |

### 1.2 数据结构对照

```
Emby                        →  飞牛影视
──────────────────────────────────────────────
Id (数字)                   →  guid (32位hex字符串)
RunTimeTicks (100ns单位)    →  duration (秒)
PlaybackPositionTicks       →  ts (秒) / watched_ts (秒)
Type: Movie/Episode/Season  →  type: Movie/Episode/Season/Tv
IndexNumber                  →  episode_number
ParentIndexNumber            →  season_number
Path                        →  files[].path (含服务器绝对路径)
MediaStreams[].Type         →  video_streams/audio_streams/subtitle_streams
SubtitleStreamIndex          →  subtitle_guid
MediaSourceId               →  media_guid
ServerId                    →  (无，通过ancestor_guid找到库位置)
```

### 1.3 认证方式

- Cookie: `Trim-MC-token` = 32位token
- Header: `Authorization: {token}` (同cookie值)
- 额外签名头: `authx: nonce={6位随机}&timestamp={13位毫秒时间戳}&sign={MD5签名}`
  - **签名算法需要逆向**（前端JS中），或者尝试仅用 cookie + Authorization 是否可以通过

### 1.4 播放流程

```
页面加载 → GET /v/api/v1/item/{guid}          获取元数据
         → GET /v/api/v1/stream/list/{guid}    获取流信息(含文件路径)
         → POST /v/api/v1/play/info            获取上次播放位置
         
点击播放 → POST /v/api/v1/play/play            获取播放链接(HLS m3u8)
         → 返回: play_link = "/v/media/{hash}/preset.m3u8"
         
播放中   → POST /v/api/v1/play/record          定时上报进度 {ts: 秒}
```

### 1.5 页面结构

- 电影详情页: `/v/movie/{guid}` — 播放按钮在 `.detail` 区域
- TV详情页: `/v/tv/{guid}` — 显示季列表
- 季详情页: `/v/tv/season/{guid}` — 显示剧集列表，「选集」区域
- 剧集详情页: `/v/tv/episode/{guid}` — 类似电影
- 视频播放页: `/v/video/{item_guid}?media_guid={media_guid}`
- 飞牛的页面使用 Vue.js 构建，SPA 路由

### 1.6 TV剧集的关键区别

飞牛的 TV 是三层结构: Tv → Season → Episode。这与 Emby 类似但数据组织方式不同:
- Season 需要通过 `/v/api/v1/season/list/{tv_guid}` 获取
- 每个 Episode 有独立的 guid
- Episode 的播放信息通过 `/v/api/v1/play/info` 获取时需要传入 episode 的 guid

---

## 二、适配方案

### 2.1 整体架构

保持与现有 embyToLocalPlayer 相同的架构模式:
```
[浏览器油猴脚本] → POST JSON → [Python HTTP Server:58000] → [调用飞牛API获取播放信息] → [启动本地播放器] → [回传进度]
```

### 2.2 需要新建/修改的文件

```
新增:
  user_script/embyToLocalPlayer_fntv.user.js   # 飞牛影视油猴脚本
  utils/fntv_api.py                             # 飞牛影视 API 封装

修改:
  utils/http_server.py   # 添加 /fnvideo 路由处理
  utils/data_parser.py   # 添加 parse_received_data_fntv() 函数
  embyToLocalPlayer_config.ini  # 添加飞牛相关配置节

可选:
  utils/players.py       # 如果播放方式有差异需要调整
```

### 2.3 第一步：油猴脚本

油猴脚本需要做的事情：
1. URL匹配: `*://*/*/v/*` (飞牛的SPA路由都在 /v/ 下)
2. 拦截播放按钮点击事件
3. 通过 cookie 读取 `Trim-MC-token` 作为认证令牌
4. 从页面URL提取 guid (如 `/v/movie/{guid}`)
5. 从 DOM 获取当前页面信息(标题、季号、集号等)
6. 调用 `/v/api/v1/play/info` 和 `/v/api/v1/stream/list/{guid}` 获取播放数据
7. 组装数据结构(模拟 Emby 的数据格式以尽可能复用后端代码)
8. POST 到 `http://127.0.0.1:58000/fnvideo/`

一种更简洁的方案：**在油猴脚本中尽量模拟 Emby 的数据格式发送给后端**，这样可以最大程度复用现有的 `data_parser.py` 和 `player_manager.py`。

但这有困难，因为飞牛的数据结构完全不同(guid vs 数字ID, 秒 vs ticks等)。

**推荐方案：直接新增飞牛专用处理路径**，避免过度耦合。核心播放逻辑可以抽取复用。

### 2.4 第二步：Python 后端

#### http_server.py 改动

```python
# 新增路由处理
if self.path.startswith('/fnvideo'):
    data = parse_received_data_fntv(json_data)
    threading.Thread(target=start_play_fntv, args=(data,), daemon=True).start()
```

#### data_parser.py 改动

新增 `parse_received_data_fntv()`:
- 输入：浏览器发来的原始数据(包含 token, guid, 当前页面类型等)
- 调用飞牛API获取详细信息: stream/list, play/info
- 输出：标准化的播放数据 dict

标准化输出格式：
```python
{
    'server': 'fntv',
    'scheme': 'https',
    'netloc': 'fn.xxx.com:4433',
    'token': 'xxx',
    'mount_disk_mode': False,  # 是否读盘模式
    'item_guid': '...',
    'media_guid': '...',
    'video_guid': '...',
    'audio_guid': '...',
    'subtitle_guid': '...',
    'file_path': '/vol2/1000/影视库/电影/xxx.mkv',  # 服务器文件路径
    'file_name': 'xxx.mkv',
    'media_title': '教父 | xxx.mkv',
    'start_sec': 99,
    'total_sec': 10528,
    'sub_file': 'http://.../subtitle/dl/xxx',  # 字幕下载URL
    'type': 'Movie',  # Movie or Episode
    # TV show 相关
    'tv_title': '...',
    'season_number': 1,
    'episode_number': 52,
    'parent_guid': '...',  # 季的guid(用于获取剧集列表)
    'list_eps': [...],  # 同季所有剧集(用于播放列表)
}
```

#### fntv_api.py (新建)

封装飞牛 API 调用:
```python
class FntvApi:
    def __init__(self, host, token):
        self.host = host
        self.token = token
        self.base = f'{host}/v/api/v1'
    
    def get_item(self, guid): ...
    def get_stream_list(self, guid): ...
    def get_play_info(self, item_guid, media_guid=None): ...
    def start_play(self, ...): ...
    def record_progress(self, item_guid, media_guid, ts, duration): ...
    def get_season_list(self, tv_guid): ...
    def get_episode_list(self, season_guid): ...
    def download_subtitle(self, subtitle_guid): ...
```

### 2.5 第三步：播放实现

#### HTTP流播放 (基础功能)
1. 调用 `POST /v/api/v1/play/play` 获取 `play_link` (m3u8地址)
2. 构建完整URL: `{scheme}://{netloc}{play_link}`
3. 将 m3u8 URL 传给本地播放器

**注意**: m3u8 HLS 流需要播放器支持。通常 mpv、VLC、PotPlayer 都支持。

#### 读盘模式 (路径转换)
1. 从 `stream/list` 获取 `file_stream.path` (服务器上的绝对路径)
2. 使用配置文件中的 `[src]`/`[dst]` 映射规则转换路径
3. 将本地路径传给播放器直接播放

#### 进度回传
1. 播放器关闭后获取停止时间(stop_sec)
2. 调用 `POST /v/api/v1/play/record` 回传进度
3. 请求体: `{"item_guid":"...","media_guid":"...","ts":<停止秒>,"duration":<总秒>}`

#### 字幕处理
1. 从 `stream/list` 获取 `subtitle_streams`
2. 文本字幕(如 srt): 通过 `GET /v/api/v1/subtitle/dl/{subtitle_guid}` 下载
3. 图形字幕(如 sup/pgs): 需要播放器支持，http流播放时内封字幕由转码服务处理

#### 播放列表
1. TV: 从当前 `episode/list` 获取同季所有剧集
2. 为每集预先请求 stream/list 获取流信息
3. 使用 PlayerManager 管理连播

---

## 三、潜在风险和待解决问题

### 3.1 authx 签名
飞牛 API 请求头中有 `authx` 签名(格式: `nonce=xxx&timestamp=xxx&sign=md5(xxx)`)。需要逆向前端JS找到签名算法。

**备选方案**:
- 如果服务端不强制校验 authx，可以只用 Authorization header
- 如果必须签名，需要找到签名密钥(可能在前端JS中硬编码)
- 或者由浏览器端(油猴脚本)代理所有API调用

### 3.2 Token 过期
`Trim-MC-token` 有过期时间，需要处理刷新机制。

**方案**:
- 浏览器端定期刷新 token 并通知后端
- 或者后端直接使用 cookie 中的所有认证信息

### 3.3 HLS 播放兼容性
飞牛使用 HLS (m3u8) 作为播放协议。某些播放器对 HLS 的支持可能有问题。

**方案**:
- mpv/VLC 对 HLS 支持良好，优先推荐
- 读盘模式下不经过 HLS，直接播放文件

### 3.4 进度回传的实时性
飞牛网页播放器在播放过程中定时调用 `/v/api/v1/play/record`，而 etlp 只在播放器关闭时回传一次。

**方案**:
- 仅在播放结束时调用 record 已足够满足需求
- 如需实时回传(跨设备继续观看场景)，可在 player_manager 中添加定时器

### 3.5 多版本视频
飞牛目前观察到的API似乎不直接支持多版本(同一 item 对应多个不同画质的文件)。但从 `stream/list` 的返回来看，一个 item 只关联一个 media_guid。

**影响**: 版本选择功能可能无法适配。

---

## 四、实施顺序建议

1. **第一阶段**: 创建油猴脚本 + 简单的 HTTP 流播放 (电影)
   - 最小可行: 点击飞牛页面播放按钮 → 本地播放器播放 m3u8 → 关闭后回传进度
   
2. **第二阶段**: TV 剧集支持 + 播放列表(连续播放)
   - 处理季/集结构
   - PlayerManager 适配
   
3. **第三阶段**: 字幕/音轨选择
   - 字幕下载和加载
   
4. **第四阶段**: 读盘模式(路径转换)
   - 配置文件扩展
   - 路径转换逻辑

5. **第五阶段**: 优化和边缘情况
   - 错误处理
   - Token 刷新
   - authx 签名处理
