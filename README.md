# Obsidian Agent MCP

用 Python 编写的本地 MCP Server，通过 Obsidian 的 [Local REST API](https://github.com/coddingtonbear/obsidian-local-rest-api) 社区插件，让 AI agent 搜索、读取和修改你的笔记。

你可以在 Obsidian 中写需求，让 agent 补充实施计划，审阅后执行，再把进度和结果写回同一篇笔记。项目使用纯本地 **stdio** 通信，由 MCP 客户端启动 Python 进程。

```text
Codex / 其他 MCP 客户端
          │ stdio
          ▼
Python MCP Server
          │ 本机 HTTP / HTTPS
          ▼
Obsidian Local REST API → 当前连接的知识库
```

## 功能

- 全文搜索、分页列出笔记、读取当前打开的笔记及反向链接。
- 创建笔记、精确替换文字、追加内容、修改标签和任务状态。
- 使用内容版本校验，发现笔记已被其他操作修改时拒绝覆盖。
- 按实施计划、代码探索、Bug 报告模板创建笔记，也支持自定义模板。
- 读取图片和 PDF 附件，上传附件并获得 Markdown 引用。
- 支持只读模式，并提供提交前、推送前及 GitHub CI 密钥检查。

项目不包含模型或聊天界面。搜索和读写由工具执行，分析需求、填写计划和编写代码由连接的 agent 完成。

## 使用前准备

1. 安装 **Python 3.11 或更新版本**、Git。
2. 在 Obsidian 中打开目标知识库，安装并启用 **Local REST API** 插件。
3. 从该知识库的插件设置中获取 API Key 和服务地址。
4. 使用支持 stdio 的 MCP 客户端。

使用期间需要保持 Obsidian、目标知识库和插件运行。不同知识库对应各自的插件服务，MCP 不会自动打开或切换知识库。

## 安装

### Windows PowerShell

```powershell
git clone https://github.com/chenjieya/obsidian-agent-mcp.git
Set-Location obsidian-agent-mcp
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
Copy-Item .env.example .env
```

### macOS / Linux

```bash
git clone https://github.com/chenjieya/obsidian-agent-mcp.git
cd obsidian-agent-mcp
python3 -m venv .venv
.venv/bin/python -m pip install -e .
cp .env.example .env
```

已有 `.env` 时保留原文件。后续示例中的路径需要替换成你的实际项目路径。

## 配置 Obsidian 连接

编辑项目根目录的 `.env`，填入自己插件中的密钥，不要使用示例值作为真实密钥。

```dotenv
OBSIDIAN_API_KEY=replace-with-your-api-key
OBSIDIAN_API_URL=https://127.0.0.1:27124
OBSIDIAN_CA_CERT=C:/path/to/obsidian-local-rest-api.crt
OBSIDIAN_VERIFY_SSL=true
OBSIDIAN_TEMPLATE_FOLDER=Templates
OBSIDIAN_READ_ONLY=false
```

### HTTPS 与证书

插件默认 HTTPS 地址为 `https://127.0.0.1:27124`。保持插件运行，在浏览器中访问其证书下载地址，将证书保存为 `.crt` 文件：

```text
https://127.0.0.1:27124/obsidian-local-rest-api.crt
```

将 `OBSIDIAN_CA_CERT` 改成下载文件的绝对路径，例如：

- Windows：`C:/code/obsidian-agent-mcp/certs/obsidian-local-rest-api.crt`
- macOS：`/Users/you/code/obsidian-agent-mcp/certs/obsidian-local-rest-api.crt`

项目直接使用该证书验证连接，不要求安装到操作系统证书库。插件重新生成证书后，需要重新下载并重启 MCP 进程。

也可以使用插件的 HTTP 服务，但需要在插件设置中启用，默认地址为 `http://127.0.0.1:27123`，此时 `OBSIDIAN_CA_CERT` 留空。不要把 HTTP 地址与默认 HTTPS 端口 `27124` 混用。`OBSIDIAN_VERIFY_SSL=false` 只跳过 HTTPS 证书验证，不会改变连接协议。

### 配置项

| 配置 | 默认值 | 含义 |
| --- | --- | --- |
| `OBSIDIAN_API_KEY` | 必填 | 当前知识库 Local REST API 的访问密钥 |
| `OBSIDIAN_API_URL` | `https://127.0.0.1:27124` | 本机插件地址，只接受回环地址 |
| `OBSIDIAN_CA_CERT` | 空 | 可选的 CA 证书文件路径 |
| `OBSIDIAN_VERIFY_SSL` | `true` | 是否验证 HTTPS 证书；指定 CA 文件时使用该 CA 验证 |
| `OBSIDIAN_TIMEOUT` | `20` | 单次 HTTP 请求超时时间，单位为秒 |
| `OBSIDIAN_TEMPLATE_FOLDER` | `Templates` | 自定义模板在知识库内的目录 |
| `OBSIDIAN_MAX_NOTE_BYTES` | `2097152` | 笔记写入及多数接口响应的大小上限，默认 2 MiB |
| `OBSIDIAN_MAX_ATTACHMENT_BYTES` | `10485760` | 附件大小上限，默认 10 MiB |
| `OBSIDIAN_MAX_SCAN_FILES` | `2000` | 递归列举目录时扫描的文件和文件夹条目上限 |
| `OBSIDIAN_READ_ONLY` | `false` | 只读模式；启用后不注册写入和打开界面的工具 |

配置只在进程启动时读取。已有环境变量优先于 `.env` 文件；修改配置后需要重新启动对应 MCP 进程。

### 检查连接

Windows：

```powershell
.\.venv\Scripts\python.exe -m obsidian_mcp --env-file .env --check
```

macOS / Linux：

```bash
.venv/bin/python -m obsidian_mcp --env-file .env --check
```

成功时会返回 `connected: true`、`authenticated: true`。可写模式还会检查插件是否支持版本条件写入；如果 `conditional_writes: false`，按提示检查或升级插件。

`--check` 完成后会退出，不会保持 MCP 服务运行，也不会修改笔记。

## 接入 MCP 客户端

在客户端创建 **STDIO** 服务，指定虚拟环境中的 Python 和 `.env` 的绝对路径。不要加 `--check`，客户端会负责启动和管理进程。

| 字段 | Windows 示例 | macOS 示例 |
| --- | --- | --- |
| Command | `C:/code/obsidian-agent-mcp/.venv/Scripts/python.exe` | `/Users/you/code/obsidian-agent-mcp/.venv/bin/python` |
| Arguments | `-m obsidian_mcp --env-file C:/code/obsidian-agent-mcp/.env` | `-m obsidian_mcp --env-file /Users/you/code/obsidian-agent-mcp/.env` |

如果客户端按数组录入参数，依次填写 `-m`、`obsidian_mcp`、`--env-file`、配置文件绝对路径四项。路径包含空格时，确保它作为一个完整参数传入。

### Codex 配置示例

在 Codex 的 MCP 配置中添加服务条目。下面使用 Windows 路径；macOS 替换为上表中的 Python 和配置文件路径。

```toml
[mcp_servers.obsidian]
command = 'C:/code/obsidian-agent-mcp/.venv/Scripts/python.exe'
args = ['-m', 'obsidian_mcp', '--env-file', 'C:/code/obsidian-agent-mcp/.env']

[mcp_servers.obsidian.env]
PYTHONUTF8 = '1'
```

JSON 格式的客户端可参考 [examples/mcp.json](examples/mcp.json)，使用前替换其中的本机路径。

### MCP Inspector

已安装兼容版本 Node.js 时，可以启动 Inspector：

```bash
npx @modelcontextprotocol/inspector
```

在界面中创建 STDIO 连接，按上表填写 Command 和 Arguments。连接后先调用 `health_check`，再调用 `read_active_note`，即可查看你在 Obsidian 中打开的笔记。

## 如何使用工具

日常使用时直接向 agent 描述任务，例如：

> 搜索知识库中与“登录失败”有关的笔记，读取相关内容并总结。

> 读取“项目/实施计划.md”，补充验收标准，保留其他内容。

在 Inspector 中测试时，则选择工具并填写其参数。

### 路径与知识库

`folder` 是已连接知识库中的子目录，`path` 是笔记或附件的相对路径。Windows 和 macOS 都使用 `/` 分隔，不需要提供知识库在磁盘上的绝对位置。

| 目标 | 参数示例 |
| --- | --- |
| 整个知识库 | `folder: ""` |
| 工作目录 | `folder: "工作"` |
| 某篇笔记 | `path: "工作/项目A/实施计划.md"` |

例如调用 `list_notes`：

```json
{
  "folder": "工作/项目A",
  "recursive": true,
  "limit": 20
}
```

调用 `read_note`：

```json
{
  "path": "工作/项目A/实施计划.md"
}
```

列表和搜索结果包含路径，随后可用它读取完整内容。当前工具参数不直接解析 `obsidian://` URI；agent 可以先提取其中的笔记位置，但必须确认 URI 指向已连接的知识库。

### 工具清单

| 工具 | 用途 |
| --- | --- |
| `health_check` | 检查 Obsidian 连接、认证和版本 |
| `read_active_note` | 读取当前打开的 Markdown 笔记 |
| `search_notes` | 关键词全文搜索，返回摘要 |
| `read_note` | 读取完整正文、属性、标签和版本 |
| `read_backlinks` | 查看哪些笔记链接到目标笔记 |
| `list_notes` | 分页列出目录内的笔记，默认包含子目录 |
| `list_notebooks` | 列出一级子文件夹 |
| `read_notebook` | 列出目录直接包含的笔记，不递归 |
| `list_tags` | 查看已有标签及使用次数 |
| `open_note` | 在 Obsidian 界面打开笔记 |
| `create_note` | 新建笔记，同路径已存在时拒绝覆盖 |
| `update_note` | 按版本替换完整文件，包括 frontmatter |
| `patch_note` | 按版本精确替换唯一匹配的文字 |
| `append_note` | 按版本追加原样内容 |
| `set_note_status` | 修改 YAML 中的任务状态 |
| `set_note_tags` | 替换 YAML 标签列表，保留正文内联标签 |
| `move_note` | 移动或重命名笔记，拒绝覆盖目标 |
| `list_templates` | 查看内置和自定义模板 |
| `read_template` | 查看模板正文及填充说明 |
| `create_from_template` | 按模板创建笔记并关联来源 |
| `create_attachment` | 从 base64 创建图片或 PDF 附件 |
| `read_attachment` | 读取附件；图片返回原生 MCP 图片块 |

修改已有笔记时，先读取返回的 `revision`，再传给修改工具。版本冲突后需要重新读取并合并，不能直接重试覆盖。少量修改优先使用 `patch_note`；`update_note` 的 `body` 是完整文件内容，必须包含需要保留的属性。

## 使用模板协作

内置模板：

| 模板 ID | 用途 |
| --- | --- |
| `builtin:implementation-plan` | 需求、相关笔记、待确认决策、实施阶段、状态和结果 |
| `builtin:codebase-exploration` | 探索目标、架构、关键流程、相关文件、风险和结论 |
| `builtin:bug-report` | 问题、复现步骤、预期与实际表现、原因、修复和验证 |

在 Inspector 中选择 `create_from_template`，填写：

```json
{
  "path": "项目/登录功能实施计划.md",
  "template_id": "builtin:implementation-plan",
  "title": "登录功能实施计划",
  "requirements": "增加记住登录状态功能，登录失败时展示错误提示。"
}
```

该操作创建的是笔记骨架。内置模板正文从带序号的二级标题开始，标题由 Obsidian 文件名展示。连接 agent 后，可要求它读取笔记、完善计划，待用户审阅后实施并回写结果。

自定义模板是 `Templates/` 中的 Markdown 笔记，`template_id` 使用其相对路径，例如 `Templates/我的计划.md`。支持 `{{title}}`、`{{requirements}}`、`{{date}}`、`{{source_template}}` 和 `variables` 中的自定义变量；缺少变量时会报错，不执行 Templater JavaScript。

`read_note` 设置 `include_template: true` 可以一起读取来源模板。客户端支持 Prompts 时，还可使用 `fill_out_template`、`implement_reviewed_plan` 两个协作提示；它们返回工作指令，由 agent 继续执行。

## 多个知识库

每个 MCP 进程连接一个知识库。为不同知识库分别准备配置文件，例如 `.env.work`、`.env.personal`，各自填写插件地址、API Key 和证书，然后在客户端新增不同名称的 MCP 服务，用 `--env-file` 指定对应文件。

多个知识库的插件同时运行时，需使用不同的可用端口。`folder` 不用于切换知识库。本项目当前没有远程 HTTP MCP 入口，服务器上的回环地址也不能直接访问你电脑里的 Obsidian。

## 开发与检查

修改代码前，安装固定版本的 Gitleaks 并启用当前仓库的 Git hooks：

```powershell
# Windows
.\.venv\Scripts\python.exe scripts/install_gitleaks.py
.\.venv\Scripts\python.exe scripts/setup_git_hooks.py
```

macOS / Linux 使用 `.venv/bin/python` 执行相同脚本。安装器校验官方归档的 SHA-256，工具放在忽略目录 `.tools/`，不修改全局 PATH。hooks 只对当前仓库生效，新克隆后需要重新启用。

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m compileall -q src scripts tests
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe scripts/check_secrets.py --worktree
```

pre-commit 检查实际暂存内容，pre-push 检查待上传的提交历史；曾经提交过密钥、随后删除的情况也会拦截。GitHub CI 配置见 [.github/workflows/ci.yml](.github/workflows/ci.yml)，远端分支合并限制还需要在 GitHub 上启用必需状态检查。

真实 `.env`、私钥、工具二进制、临时文件及本机插件证书目录均不应提交。示例、文档和测试文件也会接受内容扫描，不要填入真实密钥。

## 当前限制

- 写入依赖插件的条件写入能力，缺少能力时会拒绝写入。
- 使用相对路径标识笔记，移动后路径会变化；没有独立的数据库笔记 ID。
- 全文搜索不解析 Inkdrop 的 `book:`、`tag:` 等限定语法。
- 反向链接依赖 Obsidian 元数据缓存，索引期间可能不完整。
- 附件支持 PNG、JPEG、GIF、WebP 和 PDF；PDF 返回 base64，不进行文本抽取或 OCR。
- 自定义模板按原文展开，不自动调整已有标题或重新编号。
