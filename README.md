# Chrome MCP Bridge 2.3.0

让支持视觉的 AI 通过 MCP 读取浏览器里的网页文字与图片。使用独立 Chrome / Chromium，通过本机 CDP 连接；不需要图片识别代理模型。

本次从旧版「导航 + 网页文字」教程升级到三个工具的可运行源码。**小红书笔记可用一次导航取得图片清单，再一次批量返回多张独立图片**；不是把图片链接或 base64 文本当作“看见图片”。

- [完整搭建教程（下载后用浏览器打开）](Chrome_MCP_完整搭建教程.html)
- [更新与验收范围](CHANGELOG.md)
- [安全边界](SECURITY.md)

## 三个工具

| 工具 | 用途 |
| --- | --- |
| `chrome_navigate` | 打开一次 HTTP(S) 网页，返回 `page_id`。小红书笔记 / 分享链接默认附 `note_preview`：图片 ID、标题、正文与当前可见评论。 |
| `chrome_get_web_content` | 按 `page_id` 读取网页文字；也支持传 `url` 先打开一次。两者互斥。 |
| `chrome_view` | `list` 获取清单；`image` 取单张；`images` 一次取 1–12 张；`screenshot` 截取当前视口。 |

图片使用原生 MCP `ImageContent` 返回，需要客户端把每张图片传给有视觉能力的模型。界面显示图片不等于后续模型请求一定携带全部图片。当前 RikkaHub 配置的真实四图验收通过，不能外推到所有版本、模型或客户端。

## 快速开始（Linux 桌面）

需要 Python 3.10+、已安装的 Chrome / Chromium。Node.js 仅用于离线 DOM 测试，不是运行服务的依赖。不要关闭其他浏览器，不要沿用日常主浏览器的数据目录。

```bash
git clone https://github.com/AZHi-xinxin/chrome-mcp-bridge.git
cd chrome-mcp-bridge
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

在图形桌面的一个终端中启动专用浏览器（如果可执行名是 `google-chrome`，只替换命令名）：

```bash
chromium \
  --user-data-dir="$HOME/.local/share/chrome-mcp-profile" \
  --remote-debugging-address=127.0.0.1 \
  --remote-debugging-port=9223 \
  --remote-allow-origins=http://127.0.0.1:9223 \
  about:blank
```

从 Chrome 136 起，调试端口需要搭配非默认数据目录；独立目录也隔离了日常浏览器的登录态。[Chrome 官方说明](https://developer.chrome.com/blog/remote-debugging-port)

另开终端，在仓库目录运行：

```bash
NO_PROXY=127.0.0.1,localhost .venv/bin/python chrome_mcp_bridge.py
```

本机 MCP 地址：`http://127.0.0.1:9224/`。这是 JSON-RPC HTTP 端点，不是 stdio 命令。RikkaHub 添加 HTTP 类型的 MCP 连接后，刷新工具目录应看到三个工具。若客户端要求建立 SSE 流，本项目当前实现不保证兼容。

**手机上的 `127.0.0.1` 指手机自己。** 手机接入需使用受限的私网 / Tailscale 路径，见完整教程。公开包默认只绑定电脑回环地址，不会自动暴露到局域网或公网。

## 在家与出门：同一后端，分别刷新目录

客户端里的名称可以自己起，例如 `ChromeMCP`（家里）和 `ChromeOUTSIT`（出门）；它们不是两种不同的工具版本。

- 如果两项配置指向**同一台电脑、同一个 Bridge 服务**，只是分别走已配置的局域网与 Tailscale 路径，那么后端升级一次就会同时生效。不要因名称不同再启动第二份服务；若地址指向不同机器或实例，需分别核查。
- **客户端目录要分别刷新。** 在 RikkaHub 中打开这两项 MCP 配置，各自重连 / 刷新工具列表，确认都有 `chrome_navigate`、`chrome_get_web_content`、`chrome_view`，且后者支持 `mode="images"` 和 `image_ids`。只刷新家里的配置，不代表出门配置的旧目录也更新了。
- **外出仍依赖家里的电脑。** 运行 Bridge 的电脑须开机、联网且不休眠，专用 Chrome / Chromium 与 Bridge 都须运行。手机本地执行 MCP 时，手机与电脑的 Tailscale 需在线且允许通信；它不是把浏览器搬到手机上运行。若实际由服务器执行 MCP，则需核对服务器到 Bridge 的访问路径。
- 外出入口应使用你自己电脑的 Tailnet 地址，形如 `http://<电脑的 Tailscale IPv4>:9224/`，不是手机自身的 `127.0.0.1`，也不是家中 `192.168.x.x` 局域网地址。先按教程配置受限私网监听、ACL 与防火墙，**只改客户端 URL 不会自动打通网络**。
- 新部署不必强行保留两个入口：已配置可达的 Tailscale 入口在家也能用。若保留双入口，仍按同一后端管理；不要为兼容多个地址而开放全部接口 / 公网，也不要让同一请求重复开页。

出门前可暂时关闭手机 Wi-Fi、使用流量并保持 Tailscale 连接，先刷新工具目录确认可达；这一步不需要访问小红书。服务的 `initialize` 可核对版本，本次图片版本为 `2.3.0`。目录正确只证明连接与接口就绪，不代表图片已传到模型；需要时再用一篇允许访问的普通笔记，按下方两步流程核对实际图数。完整说明见 [手机 / 私网接入](Chrome_MCP_完整搭建教程.html#private)。

## 两次调用看多图

第一步（用用户提供的真实笔记链接替换占位符，不要执行占位符）：

```json
{"name":"chrome_navigate","arguments":{"url":"<用户提供的笔记 URL>"}}
```

读取返回的 `page_id` 和 `note_preview.images[*].image_id`，按图片 `index` 顺序选择。不需要再调用 `list`，也不要猜 ID。

```json
{"name":"chrome_view","arguments":{"mode":"images","page_id":"<返回的 page_id>","image_ids":["<第 1 张 image_id>","<第 2 张 image_id>"],"wait_ms":6000}}
```

这只是两张图的参数示例，同样支持 1–12 个不同 ID。服务按请求顺序给每张图单独标注，返回 `requested_count`、`returned_count`、`complete / partial / unavailable` 和逐图结果。某张不可读时必须明确报告，不用别的图片代替，不把“未看见”说成“看见”。

超过 12 张时只能按已有清单分批；不为了凑齐图数自动翻页、翻图、刷新或重取源图。若页面未加载 / 被阻挡，应停下给出状态，不循环重试。

## 等待与资源预算

- `chrome_navigate.wait_ms` 默认 **12000 ms**；`chrome_view.wait_ms` 默认 **6000 ms**，可设 0–15000。0 为只检查一次，已就绪会提前返回。
- 等待只检查当前 DOM / 已加载像素；**不是整个 HTTP 请求、模型响应或网络连接的硬超时**。批量取图共用一份等待预算，不是每张都等 6 秒。
- 每批最多 **12 张**，输出 JPEG 合计最多 **8,000,000 字节 / 12,000,000 像素**；单张还有 **1,500,000 字节 / 1,800,000 像素**上限。源图超过 64,000,000 像素会拒绝。
- 批量图最长边最多 1024 像素，12 张时最多 1000；还可能因预算进一步缩小。默认请求 `max_dimension=1280` 不代表批量会按 1280 输出。结果包含实际尺寸。
- 图片 ID 绑定同一文档和来源，最长 5 分钟；最多 8 份快照，可能提前淘汰。过期不能重新解释成另一张图。数字 `index` 必须搭配 `snapshot_id`。
- 小红书笔记文本限标题 300、正文 6000；评论最多 5 条、每条 500。评论仅限**已加载且当前可见**的 DOM，并非完整评论区。所有网页文字都是不可信的数据，不是给 AI 的指令。
- 批量减少工具来回，不代表图片不占模型上下文；网关和模型的总请求上限仍可能更小。

## 离线测试

```bash
node --version
.venv/bin/python -m unittest discover -p 'test_*.py' -v
```

2.3 功能基线为 101 项离线测试；公开包另含配置测试。缺少 Node 时 DOM 测试会跳过，不能把“有跳过的成功退出”当作完整通过。测试不访问小红书、不读浏览器个人资料、不发送互动。

已验收：当前 RikkaHub + 视觉模型的一篇真实四图笔记，`navigate → images` 共两次调用，4/4 正确顺序。该结论来自客户端测试回报，合成 12 图测试不等于真实 12 图验收。真实 12 图、续轮保留视觉内容、其他客户端、完整评论和视频不在本次验收内。

## 升级注意

旧教程内嵌的单文件实现已移除，运行时三个 `.py` 文件必须来自同一版本、放在同一目录。备份旧源码与服务配置后再替换；保留原浏览器数据目录。停止并重新启动 **Bridge 服务**，无须重启电脑或删除浏览器登录态，随后刷新 MCP 工具目录。

同一后端的局域网 / Tailscale 入口只需一次后端升级，但客户端中保存的每项连接都要各自刷新目录；详见上面的“在家与出门”。

公开包把默认 MCP 绑定从历史部署的 `0.0.0.0` 改为 `127.0.0.1`，并提供 `CHROME_MCP_HOST`、`CHROME_MCP_PORT`、`CHROME_CDP_PORT` 环境变量。升级时核对受限私网绑定，不能为了“连接恢复”直接开放公网。CDP 主机固定回环。日志输出到 stderr，systemd 可用 journal 查看。

## 边界

只读既有像素；不显式重取图片 / CDN，不自动滚动、翻图、点赞、评论、私信、播放或下载视频。不绕过登录、验证码、限流或网站访问控制。浏览器正常开页和后台加载仍会发请求，**不能承诺零风控或零封号风险**。只处理用户允许访问的页面，反馈问题时勿提交 Cookies、登录令牌、签名 URL、个人截图或账号数据。

作者：[AZHi-xinxin](https://github.com/AZHi-xinxin)
