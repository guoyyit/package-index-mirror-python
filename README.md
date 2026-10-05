# package-index-mirror-python

本地依赖包索引代理服务：对外提供兼容 PEP 503 的简易仓库接口，代理上游仓库的
包索引与制品下载，并按需缓存，使离线或受限网络环境下的安装与依赖解析可复现、
可验证。

## 运行

```bash
uv sync
MIRROR_UPSTREAM_URL=https://pypi.org MIRROR_CACHE_DIR=.mirror-cache \
    uv run uvicorn main:app --port 8080
```

客户端指向本服务即可（索引页中的制品链接会被改写为经本服务代理）：

```bash
pip install --index-url http://127.0.0.1:8080/simple/ <pkg>
```

## HTTP 接口

| 路由 | 说明 |
| --- | --- |
| `GET /simple/` | 根索引（项目列表），带 TTL 缓存 |
| `GET /simple/{project}/` | 项目页，链接改写为 `/artifacts/...`，带 TTL 缓存与负缓存 |
| `GET /artifacts/{project}/{filename}?u=<上游路径>&d=<算法=摘要>` | 制品下载，按需缓存 |
| `POST /admin/config` | 运行时部分更新配置（JSON 体） |
| `GET /healthz` | 健康检查 |

### 响应头语义（来源与失败原因可区分）

- `X-Mirror-Source`: `upstream`（实时获取）/ `cache`（新鲜缓存）/ `stale`
  （陈旧回退）/ `negative-cache`（负缓存命中）
- `X-Mirror-Freshness`: `fresh` / `stale`；`X-Mirror-Fetched-At` 给出原始获取时间
- 错误通过状态码 + `X-Mirror-Error` 区分：
  - `404`：上游确认不存在（或负缓存命中）
  - `502` + `integrity-failed`：制品校验失败，`X-Mirror-Reason` 给出原因
  - `503` + `upstream-unavailable`：上游不可达且无可用缓存回退

## 缓存一致性语义

- **原子发布**：制品先写入 `tmp/*.part` 并 `fsync`，再 `rename` 到
  `artifacts/`。读取方任何时刻只能看到完整内容，不会看到写了一半的文件。
- **崩溃恢复**：启动时 `recover()` 清理 `tmp/` 下的残留分片（它们从未被
  rename，因此绝不会被当作有效缓存服务出去），并丢弃损坏的元数据条目。
- **并发去重（single-flight）**：同一制品的并发请求只触发一次上游下载，
  其余等待方共享同一次获取结果（成功或失败一致）；缓存命中与直接获取的
  内容逐字节一致。
- **完整性校验**：按上游索引声明的摘要（URL fragment 中的 `sha256=...` 等）
  校验制品。校验失败的制品不进入可服务缓存，而是隔离到 `quarantine/`
  （附 `.reason` 文件），并按 `verify_retries` 限次重试；失败本身不被缓存，
  上游修复后再次请求即可成功。
- **索引元数据**：正缓存按 `index_ttl` 保鲜；过期条目保留用于
  `stale_if_error` 回退（上游不可达时返回陈旧内容并标注 `stale`）。
  上游 404 按 `negative_ttl` 负缓存，过期后自动重新询问上游，不会掩盖
  新发布的包。
- **清理策略**：总量超过 `max_cache_bytes` 时按 LRU 驱逐制品；过期负缓存
  条目被即时清理。正在下载或正在被读取的条目受引用计数保护，驱逐不会
  删除它们，进行中的安装流程不会失效。
- **配置语义**：配置是不可变快照。每个请求在开始时被赋予一份快照，
  在途请求不受后续 `POST /admin/config` 更新影响；新请求使用新快照。

## 配置项

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `upstream_url` | `https://pypi.org` | 上游仓库地址 |
| `request_timeout` | `10.0` | 上游请求超时（秒） |
| `retries` | `2` | 传输错误/5xx 的重试次数 |
| `index_ttl` | `300.0` | 索引正缓存保鲜时长（秒） |
| `negative_ttl` | `60.0` | 404 负缓存时长（秒） |
| `stale_if_error` | `true` | 上游不可达时是否回退到陈旧缓存 |
| `cache_dir` | `.mirror-cache` | 缓存根目录 |
| `max_cache_bytes` | `1 GiB` | 缓存总量上限（LRU 驱逐） |
| `verify_retries` | `1` | 校验失败后的重下次数 |

环境变量：`MIRROR_UPSTREAM_URL`、`MIRROR_CACHE_DIR`（入口 `main.py`）。

## 缓存目录布局

```
cache_dir/
  artifacts/<project>/<filename>   # 完整制品（rename 原子发布）
  tmp/*.part                       # 在途下载分片（启动时清理）
  quarantine/                      # 校验失败隔离区（附 .reason）
  meta/<key>.json + .body          # 索引元数据（含 fetched_at/expires_at）
```

## 测试

全部测试使用本地回环模拟上游，不依赖外部网络：

```bash
uv run pytest
```

覆盖：并发去重、缓存命中字节一致、校验失败隔离与限次重试、下载中途崩溃、
上游不可达（陈旧回退 / 503 区分）、负缓存、缓存上限驱逐与在途保护、
配置快照语义、重启恢复。
