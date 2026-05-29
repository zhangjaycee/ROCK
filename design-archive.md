# Sandbox Archive 设计（feature/archive）

基于 `feature/delete`。一期目标：`docker commit + ACR push` + `/data/logs OSS 上传`。
本文先对齐 Daytona 的实现，再给 ROCK 的一期落地方案，最后简评二期 `docker checkpoint`。

---

## 1. Daytona 是怎么做的（参考）

### 1.1 状态机 & 流程

Daytona 把 archive 拆成两条独立轨道，让它们异步对齐：

- **Sandbox state**：`STOPPED → ARCHIVING → ARCHIVED`（也允许 `ERROR → ARCHIVING`）
- **Backup state**（与 sandbox state 解耦）：`NONE → PENDING → IN_PROGRESS → COMPLETED / ERROR`

API 流：
1. 用户调 `/archive` → `desiredState=ARCHIVED`，`state=ARCHIVING`
2. cron `sync-stop-state-create-backups` 把 `state ∈ {ARCHIVING, STOPPED}` 且 `backupState=NONE` 的行 `setBackupPending` → 生成 `backupSnapshot = {registry}/{project}/backup-{sandboxId}:{ISO时间戳}`
3. cron `check-backup-states` 把 PENDING → 让 runner 跑 `docker commit + docker push` → 标记 `IN_PROGRESS`
4. cron `check-backup-states-in-progress` 轮询 runner 上的 backupState → COMPLETED 后回写
5. `SandboxArchiveAction.run`：发现 `backupState=COMPLETED && state=ARCHIVING` → 让 runner `destroySandbox` → DB state 改成 `ARCHIVED`

关键细节：
- runner-side: `commitContainer(snapshot) → PushImage(snapshot, registry) → RemoveImage(snapshot)`（推完删本地，免占盘）
- 每个 sandbox 保留 `existingBackupSnapshots[]` 历史，restore 时按时间倒序找第一个仍存在于 registry 的快照
- API 端用 `inspectSnapshotInRegistry()` 校验镜像在 registry 里还活着才让 restore 进入 `RESTORING`

### 1.2 restore 流（archived → 重新启动）

`sandbox-start.action.ts` 看到 sandbox 是 archived：
1. 拉 runner，先 `inspectSnapshotInRegistry` 确认镜像还在
2. 状态置 `RESTORING`，分配一个新 runner
3. runner 端 `createSandbox(backupSnapshot)` —— 把 backup 镜像当 base image 起一个新容器
4. 启动成功后 state 回到 `STARTED`

注意 Daytona **只 backup 容器 rootfs**，没有单独的"日志/数据外挂目录"备份。它把容器 fs 当唯一状态源。这点 ROCK 必须单独处理（见 §3）。

### 1.3 删除时怎么清理 ACR / registry？

Daytona 不依赖 `docker rmi remote` —— **`docker` 本身没有 remote delete 命令**。它直接调 **Docker Registry v2 HTTP API**：

```
1. GET  {registry}/v2/{project}/{repo}/tags/list           列所有 tag
2. HEAD {registry}/v2/{project}/{repo}/manifests/{tag}     取 Docker-Content-Digest
3. DELETE {registry}/v2/{project}/{repo}/manifests/{digest} 按 digest 删 manifest
```

见 `daytona/apps/api/src/docker-registry/services/docker-registry.service.ts:658-770`：

```ts
async deleteRepositoryWithPrefix(repository, prefix, registry) {
  const tagsUrl = `${registryUrl}/v2/${registry.project}/${prefix}${repository}/tags/list`
  const tagsResponse = await axios.get(tagsUrl, ...)
  for (const tag of tags) {
    const head = await axios.head(`${registryUrl}/v2/.../manifests/${tag}`, ...)
    const digest = head.headers['docker-content-digest']
    await axios.delete(`${registryUrl}/v2/.../manifests/${digest}`, ...)
  }
}
```

订阅 `SandboxDestroyedEvent` → 调用 `deleteSandboxBackupRepositoryFromRegistry`，把 `backup-*` 和 `snapshot-*` 两个 repo 的所有 tag 全删（manifest 删了之后由 registry GC 回收 blob）。

**对 ROCK 的含义**：阿里云 ACR（个人版/企业版）都支持 Docker Registry v2 HTTP API，所以同一套删除路径直接复用即可。企业版另有 [aliyun openapi](https://help.aliyun.com/document_detail/72374.html) 可按 repo 批量删 tag，但 v2 通用 API 已够用，不绑死阿里云。

---

## 2. 一期方案：commit + ACR + OSS

### 2.1 设计原则

1. **沿用现有 delete 设施**：archive 只是"删容器之前先把状态导出"，下层复用 `feature/delete` 的 STOPPED→DELETED 终态闭环。
2. **rootfs 和 /data/logs 双轨独立上传**：commit 不会捕获 bind-mount 内容，必须额外打包。
3. **不引入第二条状态机**：用 sandbox 现有状态机 + 一个 `archive_state` 字段（而不是 Daytona 的 `BackupState`）即可，避免一开始就上 Daytona 那套异步对齐复杂度。后台扫描和 retry 一期先不做。
4. **同步执行**：一期 archive 是 user-blocking 的同步 API（30s~几分钟），让我们在不引入 cron 的情况下尽快闭环。后续可以异步化。

### 2.2 状态机扩展

```
running ──stop──> stopped ──archive──> archiving ──(rootfs/logs 上传成功)──> archived
                     │                      │
                     │                      └──(任一步失败)──> stopped (保留容器，可重试)
                     │
                     └──delete──> deleted（feature/delete）
```

`archived` 是**非终态**（可 restart）；`deleted` 仍是终态。

- 入口条件：`stopped → archiving → archived`（**只允许从 stopped archive**，避免抓快照时还有进程写盘；二期看是否扩到 running checkpoint）
- 失败回滚：archive 任一步失败 → 状态回 `stopped`，容器和 logs 仍在原 runner，可重试
- 终态推进：archive 成功后默认**不自动 delete 容器**（避免还没验证镜像就把源头干掉），由 `auto_delete_seconds` 兜底或用户显式 `/delete`

### 2.3 数据模型新增字段（SandboxRecord）

| 字段 | 类型 | 说明 |
|---|---|---|
| `archive_state` | enum: none/in_progress/completed/error | 当前归档进度（一期同步执行，主要用 completed 和 error） |
| `archive_image` | str \| None | `acr.aliyuncs.com/{proj}/backup-{sandbox_id}:{timestamp}` |
| `archive_logs_object` | str \| None | `oss://{bucket}/sandbox-archive/{sandbox_id}/{timestamp}/data-logs.tar.zst` |
| `archive_time` | iso8601 \| None | 完成时间 |
| `archive_error` | str \| None | 失败原因 |

不维护 `existingBackupSnapshots[]` 历史（Daytona 那套一期不上）。每个 sandbox 最多一份归档，新 archive 覆盖旧的。

### 2.4 落地组件

#### 2.4.1 API 层
- `POST /apis/envs/sandbox/v1/archive`，参数 `sandbox_id`
- 复用 `SandboxManager.stop()` 的双跳模式：内部触发 `archive` SM 事件

#### 2.4.2 SandboxManager.archive
伪代码：
```python
async def archive(self, sandbox_id):
    sm = self._reconstruct_sm(sandbox_id)
    if sm.current_state.value != State.STOPPED:
        raise BadRequestRockError("archive requires stopped state")
    await sm.send("archive", sandbox_id, operator=self._operator,
                  meta_store=self._meta_store, archiver=self._archiver)
```

#### 2.4.3 SandboxStateMachine: on_archive

```python
def on_archive(self, sandbox_id, operator, meta_store, archiver):
    snapshot_tag = f"backup-{sandbox_id}:{datetime.now():%Y%m%dT%H%M%S}"
    spec = self.sandbox_info["spec"]
    config = DockerDeploymentConfig.from_spec(spec)

    # 1. commit + push（在 sandbox 当前所在 host 上做，operator 转发到 ray actor 或直接 worker RPC）
    image = await operator.commit_and_push(config, host_ip=..., snapshot_tag=snapshot_tag)

    # 2. 打包 /data/logs 上 OSS
    logs_object = await archiver.upload_logs(
        sandbox_id=sandbox_id,
        host_log_dir=f"{ROCK_LOGGING_PATH}/{sandbox_id}",
        host_ip=...,
    )

    # 3. 写回 archive_* 字段
    info = self.sandbox_info | {
        "state": State.ARCHIVED,
        "archive_state": "completed",
        "archive_image": image,
        "archive_logs_object": logs_object,
        "archive_time": datetime.now(...).isoformat(),
    }
    await meta_store.archive(sandbox_id, info)
```

任一步抛异常 → 状态回 stopped，写 `archive_state=error, archive_error=str(exc)`。

#### 2.4.4 Operator 新接口
- `RayOperator.commit_and_push(config, host_ip, snapshot_tag) -> image_url`
  - 把请求路由到 host_ip 的 `SandboxActor.commit_and_push.remote()`
  - `SandboxActor` 调 `DockerDeployment.commit_and_push()`
- `DockerDeployment.commit_and_push(snapshot_tag)`:
  - `docker commit {container_name} {full_image}`
  - 不需要 `docker login`（依赖 host 上的 daemon 已配置好 ACR 凭证；如果未配置则 `docker login`）
  - `docker push {full_image}` 带超时和重试
  - `docker rmi {full_image}` 清本地副本

#### 2.4.5 新组件：`rock/utils/providers/archive_provider.py`
封装 OSS 上传：
```python
class ArchiveProvider:
    def __init__(self, oss_endpoint, bucket, ak, sk, prefix="sandbox-archive"):
        self._bucket = oss2.Bucket(oss2.Auth(ak, sk), oss_endpoint, bucket)
        self._prefix = prefix

    async def upload_logs(self, sandbox_id, host_log_dir, host_ip) -> str:
        # 由 SandboxActor 端就地 tar | zstd | oss put（不要把数据流过 admin）
        # admin 端只是签发 STS 凭证或直接 PUT URL，actor 端实际上传
        ...
        return f"oss://{bucket}/{prefix}/{sandbox_id}/{timestamp}/data-logs.tar.zst"

    async def delete(self, oss_uri: str) -> None: ...
```

落地时 actor 端跑：
```bash
tar -C {host_log_dir} -cf - . | zstd -3 - | ossutil cp - oss://{bucket}/{key}
```

> 选项：复用 ROCK 已有的 `rock.sdk.envhub.datasets.registry.oss`（已 import `oss2`），不引入新依赖。

#### 2.4.6 删除归档（与 delete 联动）

`SandboxManager.delete()` 已存在。在 `on_delete` hook 里多一步：
```python
if sandbox_info.get("archive_image"):
    archive_provider.delete_image(sandbox_info["archive_image"])  # ACR v2 DELETE
if sandbox_info.get("archive_logs_object"):
    archive_provider.delete_object(sandbox_info["archive_logs_object"])
```

ACR 删 image 走 Docker Registry v2 HTTP API（与 Daytona 一致）：

```python
class AcrClient:
    async def delete_image(self, image_url: str):
        host, project, repo, tag = parse(image_url)
        # 1. HEAD manifest 取 digest
        digest = (await self._http.head(
            f"https://{host}/v2/{project}/{repo}/manifests/{tag}",
            headers={"Accept": "application/vnd.docker.distribution.manifest.v2+json",
                     "Authorization": f"Basic {creds}"},
        )).headers["Docker-Content-Digest"]
        # 2. DELETE by digest
        await self._http.delete(
            f"https://{host}/v2/{project}/{repo}/manifests/{digest}",
            headers={"Authorization": f"Basic {creds}"},
        )
```

ACR 个人版要先在控制台开启"允许 DELETE manifest"；企业版默认支持。删 manifest 后 blob 由 registry 后台 GC（可调用企业版 openapi 触发立即 GC，但一期不必要）。

### 2.5 Restore 流（archived → 新容器）

`/start` 或新 `/restore` 接收 archived sandbox：
1. SM `unarchive` 事件触发，目标 `pending`
2. 用 `archive_image` 替换 spec 里的原 image（`DockerDeploymentConfig.image = archive_image`）
3. 走标准 `start` 路径（`docker run`）
4. 在容器跑起来**之前**，actor 端下载 `archive_logs_object`、解压到新的 host log dir
5. start 完成 → state=running

restore 是新建容器（新 sandbox_id？还是复用？）：一期建议**复用 sandbox_id**，保持外部引用稳定；但内部 DB 行 `host_ip / spec.image / container_name` 必然更新。

### 2.6 Endpoint 总览

| Endpoint | 状态前置 | 状态后置 |
|---|---|---|
| `/archive` | stopped | archived（或失败回 stopped） |
| `/start`（已有，扩展） | pending（或 archived） | running |
| `/delete`（已有，扩展） | stopped / archived | deleted（顺带删 ACR + OSS） |

---

## 3. /data/logs 单独上传的必要性 & 关键细节

ROCK `DockerDeployment.run` 把 `{ROCK_LOGGING_PATH}/{container_name}` bind-mount 到容器内 `{ROCK_LOGGING_PATH}`（默认 `/data/logs`），见 `rock/deployments/docker.py:526-530`。

- bind-mount **不是 rootfs**，`docker commit` 不会包含；必须独立打包。
- XFS prjid 是 host-local 的，restore 到别的 host 时 prjid 会变；重建 quota 共享时由 `_setup_log_dir_quota_shared` 自动处理（已有逻辑）。
- tar 时建议 `--xattrs --acls` 保留权限，0o777 是 ROCK 显式 chmod 的，需要重放。

OSS 路径建议：

```
oss://{bucket}/sandbox-archive/{org_or_user}/{sandbox_id}/{timestamp}/data-logs.tar.zst
```

`{org_or_user}` 前缀方便按租户做 lifecycle policy（比如 90 天自动归档到 IA 存储）。

---

## 4. 二期前瞻：docker checkpoint（CRIU）

可行性：
- 需要 docker daemon `--experimental` + 宿主装 CRIU
- `docker checkpoint create --checkpoint-dir=/path c1 ckpt1` 把进程内存/文件描述符/网络栈 dump 到磁盘
- `docker start --checkpoint=ckpt1 c1` 从快照恢复

但和 commit 比有几个硬约束：
1. **checkpoint 数据不在 image layer 里**，是宿主 FS 的一坨二进制文件（`/var/lib/docker/.../checkpoints/{name}/`）。**没法 push 到 ACR**。只能 tar + 上 OSS（和 /data/logs 同路径，但体积更大）。
2. **跨 host restore 需要同内核版本、同 cgroup 配置、同 namespace 布局**。生产集群混合内核（5.10 / 6.x）时基本无法工作。
3. **Kata runtime 不支持 CRIU**：kata 是 microVM，进程在 guest 内核里，host CRIU 看不到。我们的 kata 路径直接放弃 checkpoint 即可，retreats to docker commit。
4. **网络/连接 state 重建脆弱**：长 TCP 连接、unix socket、文件锁全部会变成 zombie。restore 后 sandbox 多半要走 reconnect 协议。

二期建议设计：
- 复用一期的 archive 框架，引入 `archive_kind ∈ {commit, checkpoint}`
- checkpoint 路径：`tar /var/lib/docker/.../checkpoints/{name}/ + /data/logs` → 一并打到 OSS
- restore 要求**回到同一台 host**（在 spec 里冻结 host_ip，跨 host 用 commit 路径降级）
- 默认还是 commit，checkpoint 作为 opt-in（`SandboxConfig.archive_kind="checkpoint"`）

二期价值：`docker commit` 不能保留运行状态（用户的 Python REPL 内存、训练任务的优化器状态全丢）。对长跑 RL trial 有意义。

---

## 5. 与 feature/delete 的衔接

| feature/delete | feature/archive |
|---|---|
| `stop → stopped → delete → deleted` | `stopped → archive → archived → delete → deleted` |
| `auto_delete_seconds` 兜底 delete | `auto_delete_seconds` 仍然只兜底 delete；不自动 archive |
| `remove_container=True` cascade STOPPED→DELETED | archive 路径**不参与 cascade**（archived 状态本身就要保留容器到 archive 完成） |
| `DeleteReason.IMMEDIATE/EXPIRED/MANUAL` | 新增 `archive_state` 字段（与 DeleteReason 正交） |

`/delete` 对 archived sandbox 多做两件事：删 ACR image + 删 OSS object（best-effort，失败不阻塞 DB archive）。

---

## 6. 一期任务拆分

P0：
1. 状态机：新增 `archived` 状态 + `archive` 事件（`stopped → archiving → archived`，失败回 stopped）
2. DB schema：`archive_state, archive_image, archive_logs_object, archive_time, archive_error`
3. `DockerDeployment.commit_and_push(snapshot_tag)`：commit + push + 本地 rmi
4. `ArchiveProvider`（OSS tar+upload + delete）+ `AcrClient`（v2 HTTP HEAD/DELETE manifest）
5. `SandboxManager.archive()` + `/archive` API
6. `SandboxStateMachine.on_archive` 串接 1+3+4
7. `on_delete` 增加归档清理（ACR + OSS）
8. restore：`/start` 见到 `state=archived` 时走"用 archive_image 起新容器 + OSS 恢复 /data/logs"

P1：
- archive 异步化（后台 cron 推进 `archiving` 状态），不阻塞 API
- 多版本归档历史（`existingArchiveSnapshots`），restore 时按时间倒序回退
- 失败后台重试（参考 Daytona 的 retry counter 模式）

二期：docker checkpoint 路径独立 PR。

---

## 7. 决策（已对齐）

| # | 问题 | 决策 |
|---|---|---|
| 1 | archive 入口状态 | **只允许 stopped → archive**；archive 后允许 restart 回 pending |
| 2 | 多版本 vs 单版本 | **一期单版本覆盖**（新 archive 覆盖旧；不维护 `existingBackupSnapshots[]`） |
| 3 | archive 失败处理 | **回到 stopped，保留容器与本地日志**，让用户重试 |
| 4 | 用户 API | **一期不暴露 user-facing `/archive`**，先做后台触发 / 内部管控；后期对齐 Daytona 加 `/archive` + `/autoarchive/:interval` |
| 5 | ACR / OSS 凭证 & 复用 | 见 §8（重大复用机会，需要重写部分设计） |

---

## 8. ROCK 现有 archive 设施盘点（重大发现）

调研 `rock/` + `ROCK/InternalSource/` 之后，ROCK **本来就在做 log archive 的骨架**，但没接通容器 rootfs。我们 feature/archive 应该**接续而不是另起炉灶**。

### 8.1 已有的 OSS 设施

| 文件 | 状态 | 作用 |
|---|---|---|
| `rock/config.py:55-78` (`SandboxLogConfig`) | ✅ 已合入 | 字段：`archive_prefix`、`keep_days_before_archive=3`、`archive_max_attempts=3` |
| `rock/config.py:121-148` (`OssConfig` + `OssAccountConfig`) | ✅ 已合入 | 两套账号（`legacy` 兼容老 SDK；`primary` 用于"host-side archival" — 注释里就提到 archival） |
| `rock/sandbox/service/sandbox_proxy_service.py:696-747` | ✅ 已合入 | admin 端用 `aliyunsdkcore.client.AcsClient` AssumeRole 签 STS（`primary` / `legacy` 双账号） |
| `rock/sdk/sandbox/oss_client.py:144-310` | ✅ 已合入 | SDK 端 `oss2.StsAuth + resumable_upload + sign_url` 模式 |
| `rock/utils/archive_command.py` | ✅ 已写但**没人调** | `ArchiveCommand.build_key()` 和 `ArchiveCommand.build_command()`：构造 `tar -czf … && ossutil cp …` 一行命令，AK/SK 走 env 不进 argv |

**结论**：OSS 上传一期不要新写 `ArchiveProvider`，直接复用 `ArchiveCommand` + admin 端已有的 STS 签发。`SandboxLogArchiveTask` 这个名字 archive_command.py 注释里提到了但代码搜不到 —— 应该是上一轮做了一半，需要这次补齐。

### 8.2 已有的 ACR 设施

| 文件 | 状态 | 作用 |
|---|---|---|
| `ROCK/InternalSource/xrl/rock/cli/provider/acr_provider.py:1-150` | ✅ 已存在（InternalSource） | `ACRProvider` 用 `alibabacloud_cr20181201` SDK：`create_repository / get_repo_tag / repo_tag_exist / artifact_subscription_*` |
| `ROCK/InternalSource/xrl/rock/cli/command/acr.py` | ✅ 已存在 | CLI 入口，主要做镜像 transfer |
| `rock/sdk/builder/image_mirror.py` | ✅ 已存在 | `docker login + docker push` 通用代码 |
| `rock/sandbox/sandbox_actor.py:203` | ✅ 已存在 | `docker login` 单点 |

**缺口**：
- `ACRProvider` 没有 **delete tag / delete repo tag** 方法。需要新加 `delete_repo_tag(repo_namespace, repo_name, tag)`，alibabacloud_cr20181201 SDK 自带 `DeleteRepoTagRequest`。
- ACR commit 在 sandbox 容器上还没实现（DockerDeployment / SandboxActor 都没有 `commit_and_push`）。
- ACRProvider 在 **InternalSource**，开源路径不能直接 import。需要在 `rock/` 下定义抽象基类（如 `AbstractImageRegistry`），InternalSource 提供 ACR 实现，OpenSource 提供基于 Docker Registry v2 HTTP API 的 fallback 实现（与 Daytona 路径一致）。

### 8.3 凭证来源（直接复用）

OSS：admin 已经签 STS，actor 端从 admin 拿 `Credentials` + bucket + endpoint，注入到 `ArchiveCommand.build_command` 用的 env，不需要新加凭证存储。  
ACR：
- 阿里云 ACR push 走 docker push：复用 `image_mirror.py` 的 docker login 模式，凭证 AK/SK 在 YAML 里（`OssConfig` 旁加 `AcrConfig`）。
- ACR delete 走 OpenAPI：直接复用 InternalSource `ACRProvider` 的 `Client(open_api_models.Config)` 模式。

---

## 9. 一期方案（基于 §8 修订）

### 9.1 复用矩阵

| 能力 | 复用现成 | 需要新写 |
|---|---|---|
| OSS 上传 /data/logs | ✅ `ArchiveCommand` + admin STS | 把 `SandboxLogArchiveTask` 真正接进生命周期 |
| OSS 删除归档 | （admin 端 oss2.Bucket.delete_object） | 新写薄封装 `ArchiveProvider.delete_logs(oss_uri)` |
| docker commit + push | ✅ `image_mirror.py` 的 login + push | `DockerDeployment.commit_and_push(snapshot_tag)` + actor 路由 |
| ACR delete tag | ✅ `ACRProvider`（InternalSource） | 新加方法 `delete_repo_tag` + OpenSource 走 v2 HTTP API fallback |
| 状态机 archive 事件 | （已有 SM 框架） | `on_archive` hook |
| DB 字段 | （SandboxRecord） | `archive_state, archive_image, archive_logs_object, archive_time, archive_error` |
| 内部触发器 | （已有 BaseManager 后台扫描） | `_check_archive_background`（按 `SandboxLogConfig.keep_days_before_archive` 扫 stopped） |
| 用户 API | — | **一期不做** |

### 9.2 触发路径（一期，无用户 API）

`SandboxLogConfig.keep_days_before_archive=3` 已经提示了设计意图：stop 后 3 天的 sandbox 自动归档。所以一期就是：

1. 后台 cron `_check_archive_background`（与 `_check_delete_background` 并列，BaseManager 上面挂）扫 `state=stopped && stop_time + keep_days_before_archive < now` 的 sandbox
2. 每条触发 `SandboxManager.archive(sandbox_id, reason=ArchiveReason.EXPIRED)`
3. archive 成功 → state=archived；失败 → 回 stopped + 计数 `archive_attempts`，达到 `archive_max_attempts` 后停下，下次再扫继续
4. 由 `auto_delete_seconds` 兜底，archived 状态在更久之后再 delete（顺带清 ACR + OSS）

restart 入口：复用 `/start`，当 state=archived 时走 unarchive 路径。

### 9.3 状态机变化（修订）

```
running ──stop──> stopped ──(后台 archive)──> archiving ──> archived
                     ↑                            │              │
                     └──(失败回退/重试)────────────┘              │
                                                                 │
        ┌──restart──> pending ──> running <───(restart from)─────┘
        │
   archived
```

- `stopped → archive` 事件：用 `archiver` 注入（OSS uploader + ACR pusher）
- `archived → restart`：与 `stopped → restart`（已存在）共享 `restart` 事件，但 on_restart hook 内部区分：从 archived 来时先 pull `archive_image` + 下载 `archive_logs_object` 解压

### 9.4 抽象层：避免 OpenSource 引用 InternalSource

```
rock/sandbox/archive/
├── __init__.py
├── provider.py         # AbstractArchiveProvider（commit_and_push, delete_image, upload_logs, delete_logs, download_logs, pull_image）
├── oss_logs.py         # 用 ArchiveCommand + admin STS 实现 upload/delete/download
└── registry/
    ├── __init__.py
    ├── v2_http.py      # Docker Registry v2 HTTP API 删 manifest（OpenSource fallback，与 Daytona 一致）
    └── (InternalSource: xrl/rock/archive/acr_openapi.py 用 ACRProvider 实现，覆盖默认路径)
```

工厂在 `rock_config.archive_provider` 选 `acr` 还是 `v2`，让 InternalSource 通过 YAML 切到 `acr` 走 ACRProvider。

---

## 10. 一期任务拆分（修订）

P0：
1. **状态机**：新增 `archived` 状态 + `archive` 事件；on_archive hook
2. **DB schema**：`archive_state, archive_image, archive_logs_object, archive_time, archive_error, archive_attempts`
3. **`DockerDeployment.commit_and_push(snapshot_tag)`**：commit → push（复用 `image_mirror.py` 模式）→ 本地 rmi
4. **`SandboxLogArchiveTask` 接通**：复用 `ArchiveCommand.build_command`，admin 端签 STS 注入 env，runtime.execute 跑 tar+ossutil
5. **`AbstractArchiveProvider` + `V2HttpRegistry`（OpenSource 默认）+ `ACRProvider.delete_repo_tag`（InternalSource 扩展）**
6. **`SandboxManager.archive()` + `_check_archive_background`** 后台 cron
7. **on_delete 联动**：调 archive_provider.delete_image + delete_logs
8. **`restart` 扩展**：on_restart 区分 from-stopped 和 from-archived；from-archived 时先 pull image + restore /data/logs

P1：
- 用户 API（`/archive` + `/autoarchive/:interval`）对齐 Daytona
- 多版本归档历史
- archive 异步化（cron 推进状态，不阻塞调用方）
- 失败后台重试 + retry counter（参考 Daytona）

二期：
- docker checkpoint（见 §4）
- ACR 凭证轮转 / RAM Role

