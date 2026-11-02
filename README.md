# AstrBot × Dify Cloud Long-Term Memory Plugin (`astrbot_plugin_dify_memory`)

<p align="center">
  <a href="#简体中文"><b>简体中文</b></a> | <a href="#english"><b>English</b></a>
</p>

---

<a id="简体中文"></a>

## 🇨🇳 简体中文

利用 **Dify 云端 5GB 免费知识库（Dataset）**（或私有部署 Dify）作为 AstrBot 的云端长期向量记忆库，实现**以用户（QQ 号 / 平台 User ID）为单位跨群互通**的长期记忆读写、后台自动总结归档与混合检索注入。

### ✨ 核心特性

1. **以用户为单位跨群互通 & 严格隔离**
   - 每个 `user_id` 对应 Dify 知识库中的一个独立文档 `mem_user_{user_id}.txt`。
   - 每条提炼出的记忆作为该文档下的独立分段（Segment）存储，自动携带 `[USER:{user_id}] [TIME:YYYY-MM-DD]` 标签与关键词（Keywords）。
   - 召回时双重校验 `document_id` 与 `[USER:{user_id}]` 标记，确保用户在任意群聊、私聊中共享自己的专属长期记忆，且不同用户之间严格隔离、绝不串台。

2. **每 N 轮对话后台自动总结归档（零阻塞）**
   - 直接接入 **AstrBot 已配置的模型提供商**：可在插件配置中下拉选择专门用于后台总结的轻量模型（如 DeepSeek、GPT-4o-mini、Gemini Flash 等），留空则自动复用当前会话模型。
   - **WebUI 自定义记忆提炼提示词**：内置 `summary_prompt` 多行文本框，可自由定制希望记住或忽略的内容（默认支持提取用户画像、偏好以及用户对 Bot 的态度/夸奖/冒犯/约定），底层自动拼接 JSON 数组输出约束，无需担心改坏格式。
   - 当某用户累计对话达到 `summary_turns`（默认 10 轮）时，后台异步任务自动提炼事实与偏好并写入 Dify，不阻塞正常聊天响应。
   - 若 N 轮对话均为日常寒暄无有效长期信息，自动跳过写入；若遇网络波动导致写入失败，自动将对话轮次恢复至本地缓冲区防止丢失。

3. **完善的手动管理指令集**
   - `/mem status`：查看当前用户已绑定的 Dify Document ID、待归档缓冲轮数（如 `4 / 10 轮`）及连接状态。
   - `/mem force`：不等待凑满 N 轮，立即将当前缓冲区的对话强制总结并归档到 Dify。
   - `/mem add <内容>`：手动添加一条关于自己的长期记忆。
   - `/mem search <关键词>`：测试从 Dify 检索自己的长期记忆并展示相关度分数。
   - `/mem clear`：删除自己在 Dify 云端的记忆文档并清空本地未归档缓冲。

### 🚀 Dify 知识库初始化指南

> ⚠️ **重要前提**：必须创建 **经典知识库（Classic Knowledge Base）**，**不要**创建“智能体知识库（Agent Knowledge Base）”或外部知识库。只有经典知识库完整开放了底层的文档创建（`create-by-text`）与分段追加（`segments`）API。

1. 登录 [Dify Cloud](https://cloud.dify.ai/)（或你的私有部署 Dify），进入顶部的 **知识库（Knowledge）** 页面。
2. 点击 **创建知识库** -> 选择创建 **经典知识库（Classic）** -> 点击 **创建一个空知识库**，命名为如 `astrbot-user-memories`。
   - **索引方式**：推荐选择 **高质量（High Quality）**，并选定 Embedding 模型（如 `BAAI/bge-m3` 或 `gemini-embedding-2-preview`）。
   - **检索设置**：推荐开启 **混合检索（Hybrid Search）** 并配置 **Rerank 模型**（如 `Pro/BAAI/bge-reranker-v2-m3`），Score 阈值建议设为 `0.40` ~ `0.45`。
3. 获取 **Dataset ID**：打开该经典知识库，浏览器地址栏 URL 中 `/datasets/<UUID>/documents` 的 `<UUID>` 即为 `dify_dataset_id`。
4. 获取 **API Key**：在知识库页面点击 **服务 API / API 访问（API Access）** -> **API 密钥**，生成一个 `dataset-...` 开头的密钥，填入 `dify_dataset_api_key`。

### ⚙️ 插件安装、更新与配置

#### 1. 安装方式
- **方式 A（GitHub 仓库安装）**：在 AstrBot WebUI 的 **插件管理 -> 安装插件** 中输入仓库地址：
  `https://github.com/zaixiakongyiji/astrbot_plugin_dify_memory`
- **方式 B（ZIP 压缩包离线上传）**：将 `astrbot_plugin_dify_memory.zip` 在 AstrBot WebUI 的 **插件管理 -> 上传插件** 中直接选择安装。
- **方式 C（目录复制）**：将 `astrbot_plugin_dify_memory` 文件夹复制到 AstrBot 的 `data/plugins/` 目录下并重载插件。

#### 2. 插件更新方法（保留已有配置与本地缓冲）
- 插件的所有运行数据（用户文档映射与对话缓冲）均存储在独立的 `data/plugin_data/astrbot_plugin_dify_memory/` 下，配置文件存储在 `data/config/` 下。无论是通过 WebUI 在线更新、卸载后重新上传新版 `.zip`，还是直接覆盖目录文件并点击 **重载（Reload）**，均**不会丢失**已配置的 API Key 和已生成的记忆文档映射。

#### 3. 配置项说明
| 配置项 | 类型 | 默认值 | 说明 |
| :--- | :--- | :--- | :--- |
| `dify_base_url` | `string` | `https://api.dify.ai/v1` | Dify API 基础地址（支持官方云端或私有部署） |
| `dify_dataset_api_key` | `string` | `""` | Dify 知识库 `dataset-...` 专用密钥 |
| `dify_dataset_id` | `string` | `""` | 目标 **经典知识库** 的 UUID |
| `summary_provider_id` | `string` | `""` | 后台记忆总结专用模型提供商（下拉选择，留空则自动复用当前对话模型） |
| `summary_turns` | `int` | `10` | 自动总结触发轮数（N 轮） |
| `summary_prompt` | `text` | *内置默认提示词* | 记忆提炼系统提示词（支持在 WebUI 自定义提炼标准，底层自动附加 JSON 格式约束） |
| `retrieve_top_k` | `int` | `5` | 每次对话前从 Dify 检索返回的最大候选记忆条数 |
| `score_threshold` | `float` | `0.45` | 检索相似度阈值（`0.0` ~ `1.0`） |
| `search_method` | `string` | `dataset_default` | 检索模式（推荐 `dataset_default`，直接沿用你在 Dify WebUI 设置的混合检索与 Rerank 模型） |
| `min_query_length` | `int` | `2` | 触发记忆检索的最小消息字数（过滤单字或纯标点以节省延迟） |
| `request_timeout` | `int` | `15` | Dify API 请求超时秒数 |

---

<a id="english"></a>

## 🌐 English

Leverage **Dify Cloud's 5GB Free Knowledge Base (Dataset)** (or self-hosted Dify) as a cloud-based long-term vector memory store for **AstrBot**. It provides **cross-group, per-user (`user_id` / QQ ID)** long-term memory persistence, non-blocking background conversation summarization, and hybrid retrieval injection.

### ✨ Key Features

1. **Cross-Group User-Level Memory & Strict Isolation**
   - Each `user_id` maps to a dedicated document `mem_user_{user_id}.txt` inside your Dify Dataset.
   - Each extracted memory is stored as an independent **Segment** tagged with `[USER:{user_id}] [TIME:YYYY-MM-DD]` and associated keywords.
   - Dual verification on both `document_id` and `[USER:{user_id}]` tags during retrieval ensures a user's memories seamlessly follow them across any group or private chat while remaining strictly isolated from other users.

2. **Background Auto-Summarization Every N Turns (Zero Blocking)**
   - **Native AstrBot LLM Provider Integration**: Select a dedicated lightweight model (e.g., DeepSeek, GPT-4o-mini, Gemini Flash) from the WebUI dropdown for background summarization, or leave it blank to automatically reuse the active chat session's provider.
   - **Customizable Extraction Prompt (`summary_prompt`)**: Freely customize what the Bot should remember or ignore (facts, preferences, user attitude, praise/offense, inside jokes) via a multi-line textarea in the WebUI. The plugin automatically appends strict JSON array formatting constraints under the hood.
   - Once a user accumulates `summary_turns` (default: 10 turns), an asynchronous background task extracts long-term memories and pushes them to Dify without delaying chat responses.
   - Casual small talk with no long-term value is automatically skipped. If a network error occurs, turns are safely restored to the local buffer.

3. **Built-in Memory Management Commands**
   - `/mem status`: View your bound Dify Document ID, buffered turn count (e.g., `4 / 10`), and connection status.
   - `/mem force`: Force immediate summarization and archiving of buffered turns without waiting for N turns.
   - `/mem add <content>`: Manually add a long-term memory for yourself.
   - `/mem search <query>`: Test retrieving your memories from Dify and inspect relevance scores.
   - `/mem clear`: Delete your cloud memory document on Dify and reset your local buffer.

### 🚀 Dify Knowledge Base Setup Guide

> ⚠️ **Important Prerequisite**: You MUST create a **Classic Knowledge Base** in Dify, **NOT** an "Agent Knowledge Base" or External Knowledge Base. Only Classic Knowledge Bases expose the `create-by-text` and `segments` write APIs required for dynamic memory storage.

1. Log in to [Dify Cloud](https://cloud.dify.ai/) (or your self-hosted instance) and navigate to **Knowledge** at the top.
2. Click **Create Knowledge** -> Choose **Classic Knowledge Base** -> Click **Create an empty knowledge base** (e.g., `astrbot-user-memories`).
   - **IndexingMode**: Select **High Quality** and choose an Embedding model (e.g., `BAAI/bge-m3` or `gemini-embedding-2-preview`).
   - **Retrieval Setting**: Enable **Hybrid Search** and configure a **Rerank model** (e.g., `Pro/BAAI/bge-reranker-v2-m3`), with Score Threshold around `0.40` ~ `0.45`.
3. **Get Dataset ID**: Open the knowledge base and copy the `<UUID>` from the browser URL `/datasets/<UUID>/documents` into `dify_dataset_id`.
4. **Get API Key**: Click **Service API / API Access** -> **API Keys**, generate a `dataset-...` secret key, and paste it into `dify_dataset_api_key`.

### ⚙️ Installation, Updating & Configuration

#### 1. Installation
- **Option A (From GitHub URL)**: In AstrBot WebUI -> **Plugin Management -> Install Plugin**, enter:
  `https://github.com/zaixiakongyiji/astrbot_plugin_dify_memory`
- **Option B (Offline ZIP Upload)**: Upload `astrbot_plugin_dify_memory.zip` via **Plugin Management -> Upload Plugin** in the AstrBot WebUI.
- **Option C (Manual Copy)**: Copy the `astrbot_plugin_dify_memory` folder into `AstrBot/data/plugins/` and reload plugins.

#### 2. Updating (Preserves Config & User Data)
- All persistent mappings (`user_documents.json`) and turn buffers (`turn_buffers.json`) are stored inside `data/plugin_data/astrbot_plugin_dify_memory/`, while plugin settings reside in `data/config/`. Updating via GitHub, uninstalling and re-uploading a new `.zip`, or overwriting the plugin directory will **never** erase your saved API keys or existing user memory mappings.

#### 3. Configuration Reference
| Key | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `dify_base_url` | `string` | `https://api.dify.ai/v1` | Dify API base URL (Cloud or self-hosted `/v1` endpoint) |
| `dify_dataset_api_key` | `string` | `""` | Dify Dataset API Key (`dataset-...`) |
| `dify_dataset_id` | `string` | `""` | Target **Classic Knowledge Base** UUID |
| `summary_provider_id` | `string` | `""` | Dedicated LLM provider for summarization (leave empty to reuse current chat model) |
| `summary_turns` | `int` | `10` | Number of conversation turns per user before triggering auto-summarization |
| `summary_prompt` | `text` | *Built-in prompt* | System prompt for memory extraction (JSON array format constraint is appended automatically) |
| `retrieve_top_k` | `int` | `5` | Maximum number of memory segments retrieved per turn |
| `score_threshold` | `float` | `0.45` | Minimum relevance score threshold (`0.0` ~ `1.0`) |
| `search_method` | `string` | `dataset_default` | Retrieval mode (`dataset_default` uses your Dify WebUI Hybrid Search & Rerank settings) |
| `min_query_length` | `int` | `2` | Minimum message length to trigger Dify retrieval |
| `request_timeout` | `int` | `15` | HTTP request timeout in seconds for Dify API calls |
