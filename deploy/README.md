# 云端部署准备：铁底湾多人网推

目标服务器目前没有域名，首轮可直接通过公网 IP 访问 `http://<服务器公网IP>/tiedi/`。后续绑定域名并启用 HTTPS 后，再切换到正式入口。**本说明是部署手册，不代表已经连接或上传到云端。**

## 架构与运行边界

- Nginx 接收 HTTP 或 HTTPS，并把 `/tiedi/` 反代到 `127.0.0.1:8001`；FastAPI 同进程托管 `frontend/dist`、API 和舰船图片。
- systemd 单进程运行。平台会话 token 只保存在进程内，重启会要求重新登录；不要启用多个 Uvicorn worker。
- 程序代码放 `/opt/tiedi`，SQLite 和战报文件放 `/var/lib/tiedi`。服务以无登录 shell 的 `tiedi` 系统用户运行，不用 root。
- 首次创建裁判账号必须通过服务器本机交互命令；公网 `/platform/users` 不允许无认证创建首个裁判。
- 多人局禁止通过旧 `/games/{id}` 路由访问，旧 `/games` 列表也不包含网推对局；请使用带身份验证的 `/platform/...` API。

## 上传前检查

1. 先在本地通过平台/引擎测试、前端构建及生产路径静态构建检查。
2. 确认当前测试库**不随代码上传**。SQLite 中包含测试对局、裁判密码哈希，以及裁判分发玩家账密所需的可恢复密码；备份也必须按秘密材料加密保管。
3. 不上传 `.env`、API 密钥、`tmp/`、`artifacts/`、`node_modules/`、`frontend/dist/`、本地 SQLite、SQLite WAL/SHM、`backend/reports/` 或本地虚拟环境。
4. 服务器首次上线使用新的 `/var/lib/tiedi/state.sqlite3`；请勿把开发库复制到公网环境。

## 服务器准备

使用受支持的 Linux 发行版，安装 Python 3.11+、Node.js 20+、Nginx、rsync 和 SQLite CLI。IP 临时验收阶段安全组仅开放 SSH 和 80；8001 不对公网开放。HTTP 不加密，期间只用测试账密和测试数据，勿发送真实玩家凭据。正式对外使用前应绑定域名并启用 HTTPS（443）。

```bash
sudo useradd --system --home-dir /opt/tiedi --shell /usr/sbin/nologin tiedi
sudo install -d -o root -g tiedi -m 0750 /opt/tiedi
sudo install -d -o tiedi -g tiedi -m 0750 /var/lib/tiedi
```

## 上传与构建

在本机仓库根目录执行；不要加 `--delete`，避免误删服务器上的站点文件：

```bash
SERVER_IP=YOUR_SERVER_PUBLIC_IP  # 替换为服务器公网 IP
rsync -av \
  --exclude .git --exclude .venv --exclude node_modules --exclude dist \
  --exclude tmp --exclude artifacts --exclude backend/reports \
  --exclude 'backend/*.sqlite3*' --exclude '.env' --include '.env.example' --exclude '.env.*' \
  ./ root@"$SERVER_IP":/opt/tiedi/
```

`.env.example` 仅是空模板；真实 `.env` 文件不会上传。上传后检查 `/opt/tiedi` 所有源码和资源可被 `tiedi` 读取、不可由服务用户修改。

服务器端安装依赖并用固定 lockfile 构建子路径版本：

```bash
cd /opt/tiedi
python3.11 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install .
cd frontend
corepack pnpm install --frozen-lockfile
corepack pnpm exec tsc -b
corepack pnpm exec vite build --base=/tiedi/
cd /opt/tiedi
```

如果服务器不允许联网安装前端依赖，可在可信构建机用同一 lockfile 构建 `frontend/dist`，再单独上传构建产物；不要提交构建产物到仓库。

## 初始化首个裁判

先在服务器设置好状态目录权限，然后**在服务器 SSH 交互终端**运行。命令会用隐藏输入读取口令，只写入 scrypt 哈希，不会把密码放在 shell 历史或命令参数中：

```bash
cd /opt/tiedi
sudo -u tiedi env IBS_DB_PATH=/var/lib/tiedi/state.sqlite3 \
  /opt/tiedi/.venv/bin/python -m iron_bottom_sound.platform.bootstrap_user
```

此操作只能初始化空平台数据库；已有首个账号后，其他用户从裁判端创建。为裁判设置独立强密码，不复用测试环境账密。

## 密钥与服务

DeepSeek 密钥为可选项；不配置时，基础对局可运行，依赖外部模型的功能不可用。若要启用，单独创建 root-only 文件：

```bash
sudo install -o root -g root -m 0600 /dev/null /etc/tiedi.env
sudoedit /etc/tiedi.env
```

只在该文件中填写 `DEEPSEEK_API_KEY=...`，不要把密钥贴进命令、仓库、工单或日志。

```bash
sudo install -o root -g root -m 0644 /opt/tiedi/deploy/tiedi.service /etc/systemd/system/tiedi.service
sudo systemctl daemon-reload
sudo systemctl enable --now tiedi
systemctl status tiedi --no-pager
journalctl -u tiedi -n 50 --no-pager
```

## Nginx 与限速

### 暂无域名：公网 IP 临时访问

把 `deploy/nginx-tiedi-http.conf` 的 `limit_req_zone` 放在 Nginx `http {}` 上下文（常见为 `/etc/nginx/conf.d/`），再把 `deploy/nginx-tiedi-ip.conf` 放入 `/etc/nginx/sites-available/` 并启用。配置会监听 80 端口并代理 `/tiedi/`，访问地址为 `http://<服务器公网IP>/tiedi/`。若机器上已有默认站点，先确认不会产生重复的 `default_server`；可将配置中的 `listen 80 default_server` 改为 `listen 80`，并设置合适的 `server_name`。

### 绑定域名之后：正式 HTTPS 入口

先为域名签发并配置有效证书，再把 `deploy/nginx-tiedi-http.conf` 的限速区放在 Nginx `http {}` 上下文，并将 `deploy/nginx-tiedi.conf` 的两个 location 放入对应 HTTPS `server {}`。精确登录 location 为登录接口启用每 IP 限速；通用 location 处理 SPA、API、图片及战报下载。

配置完成后校验并平滑加载：

```bash
sudo nginx -t
sudo systemctl reload nginx
```

## 上线验收

IP 临时验收：

```bash
curl -fsS -o /dev/null -w '%{http_code}\n' http://<服务器公网IP>/tiedi/                 # 200
curl -fsS http://<服务器公网IP>/tiedi/ | grep -o '/tiedi/assets/[^" ]*' | head -3       # 资源前缀
curl -fsS http://<服务器公网IP>/tiedi/api/scenarios >/dev/null                            # 200 JSON
```

域名和 HTTPS 配置完成后，再执行正式入口验收：

```bash
curl -fsS -o /dev/null -w '%{http_code}\n' https://<你的域名>/tiedi/                 # 200
curl -fsS https://<你的域名>/tiedi/ | grep -o '/tiedi/assets/[^" ]*' | head -3
curl -fsS https://<你的域名>/tiedi/api/scenarios >/dev/null
curl -fsS -o /dev/null -w '%{http_code}\n' 'https://<你的域名>/tiedi/assets/counters/DD标记.png' # 200
```

另在公网浏览器检查：未登录 `/platform/users` 返回 401；登录、创建剧本、创建对局、四个玩家席位登录、提交订单、裁判审理/重骰/发布、电报与逐回合存档下载。用一局测试数据验证旧 `/api/games` 列表不显示网推局，访问旧 `/api/games/{game_id}/view?debug=true` 返回 404；不能只凭前端隐藏控件判定安全。

## 备份与更新

- 备份前停止服务，使用 SQLite `.backup` 或 `VACUUM INTO` 生成一致快照，再备份 `/var/lib/tiedi/reports/`；加密后存到服务器外的受控位置。定期演练恢复。
- 数据库保存用户哈希、对局状态和裁判可查看的玩家分发密码；把数据库与备份视同含凭据的秘密文件，限制为 `tiedi` 可读，异地副本加密。
- 更新只覆盖 `/opt/tiedi` 代码与静态资源，保留 `/var/lib/tiedi`；重建前端后重启服务。回滚前也先备份数据库。
- 改动 Nginx 才需要 `nginx -t` 和 reload；服务日志只通过 journald 查看，禁止记录 Authorization header 或口令。

## 当前准备状态

已准备：多人局旧接口隔离、首裁判离线初始化、非 root systemd 单元、登录限速片段、独立数据目录、无域名 IP 临时验收配置、正式 HTTPS 迁移步骤和备份/验收步骤。云端上传、服务器配置和公网浏览器验收仍待执行；本批没有连接服务器、创建云资源或上传文件。

本地数据库目前还含有 5 个自定义想定和 8 个网推对局。按本手册全新建库部署时，这些内容不会自动出现；正式上传前需明确要在云端重新制作哪些想定/对局。不要为省事直接覆盖部署数据库或把整份开发库当作代码上传包。
