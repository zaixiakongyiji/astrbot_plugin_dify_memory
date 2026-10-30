# AstrBot × Dify 云端长期记忆插件 (`astrbot_plugin_dify_memory`)

利用 **Dify 云端 5GB 免费知识库（Dataset）** 作为 AstrBot 的云端长期向量记忆库，实现**以用户（QQ号 / 平台 User ID）为单位跨群互通**的长期记忆读写与自动归档。

---

## ✨ 核心特性

1. **以用户为单位跨群互通 & 严格隔离**
   - 每个 `user_id` 对应 Dify 知识库中的一个独立文档 `mem_user_{user_id}.txt`。
   - 每条提炼出的记忆作为该文档下的独立分段（Segment）存储，自动携带 `[USER:{user_id}] [TIME:YYYY-MM-DD]` 标签与关键词（Keywords）。
   - 召回时双重校验 `document_id` 与 `[USER:{user_id}]` 标记，确保用户在任意群聊、私聊中共享自己的专属长期记忆，且不同用户之间严格隔离。

2. **每 N 轮对话后台自动总结归档（零阻塞）**
   - 直接接入 **AstrBot 已配置的模型提供商**：可在插件配置中指定专门用于后台总结的轻量模型（如 DeepSeek、GPT-4o-mini、Gemini Flash 等），留空则自动复用当前会话模型。
   - 当某用户累计对话达到 `summary_turns`（默认 10 轮）时，后台异步任务自动提炼事实与偏好并写入 Dify，不阻塞正常聊天响应。
   - 若 N 轮对话均为日常寒暄无有效长期信息，自动跳过写入；若遇网络波动导致写入失败，自动将对话轮次恢复至本地缓冲区防止丢失。

3. **完善的手动管理指令集**
   - `/mem status`：查看当前用户已绑定的 Dify Document ID、待归档缓冲轮数（如 `4 / 10 轮`）及连接状态。
   - `/mem force`：不等待凑满 N 轮，立即将当前缓冲区的对话强制总结并归档到 Dify。
   - `/mem add <内容>`：手动添加一条关于自己的长期记忆。
   - `/mem search <关键词>`：测试从 Dify 检索自己的长期记忆并展示相关度分数。
   - `/mem clear`：删除自己在 Dify 云端的记忆文档并清空本地未归档缓冲。

---

## 🚀 Dify 知识库初始化指南

> ⚠️ **重要前提**：必须创建 **经典知识库（Classic Knowledge Base）**，**不要**创建“智能体知识库（Agent Knowledge Base）”或外部知识库。只有经典知识库完整开放了底层的文档创建（`create-by-text`）与分段追加（`segments`）API。

1. 登录 [Dify Cloud](https://cloud.dify.ai/)（或你的私有部署 Dify），进入顶部的 **知识库（Knowledge）** 页面。
2. 点击 **创建知识库** -> 选择创建 **经典知识库**（导入已有文本或创建空知识库），命名为如 `astrbot-user-memories`。
   - **索引方式**：推荐选择 **高质量（High Quality）**（配置 Embedding 模型，如 `Qwen/Qwen3-Embedding-8B`）。
   - **检索设置**：推荐开启 **混合检索（Hybrid Search）** 并配置 **Rerank 模型**（如 `Pro/BAAI/bge-reranker-v2-m3`）。
3. 获取 **Dataset ID**：打开该经典知识库，浏览器地址栏 URL 中 `/datasets/<UUID>/documents` 的 `<UUID>` 即为 `dify_dataset_id`。
4. 获取 **API Key**：在知识库页面左侧或右上角点击 **API 访问（API Access）**，点击右上角 **API 密钥** 生成一个 `dataset-...` 开头的密钥，填入 `dify_dataset_api_key`。

---

## ⚙️ 插件安装、更新与配置

### 1. 首次安装
- **方式 A（推荐：ZIP 压缩包上传）**：将 `astrbot_plugin_dify_memory` 打包为 `.zip` 压缩包，在 AstrBot WebUI 的 **插件管理 -> 右上角上传插件** 中直接选择 ZIP 安装。
- **方式 B（目录复制）**：将 `astrbot_plugin_dify_memory` 文件夹复制到 AstrBot 的 `data/plugins/` 目录下并重启/重载插件。

### 2. 插件更新方法（保留已有配置与本地缓冲）
- **如果你是通过目录部署**：直接用新版本的源码文件覆盖 `data/plugins/astrbot_plugin_dify_memory/` 目录下的同名文件，然后在 AstrBot WebUI **插件管理** 中找到本插件点击 **重载（Reload）** 即可。
- **如果你是通过 ZIP 压缩包上传安装**：
  1. 重新将 `astrbot_plugin_dify_memory` 打包为 `.zip`。
  2. 在 AstrBot WebUI **插件管理** 中，可以先点击本插件的 **卸载**（卸载插件**不会**删除 `data/plugin_data/astrbot_plugin_dify_memory/` 下的用户文档映射与缓冲数据，也不会清空 `data/config/` 下已保存的插件配置；或者也可以直接再次上传同名 ZIP 覆盖安装），然后重新上传新版 `.zip`。
  3. 更新后打开插件配置，确认 **`dify_dataset_id`** 已更新为你新建的**经典知识库 UUID**，并将 **`search_method`** 设为 `dataset_default` 保存即可。

### 3. 配置项说明
- **Dify API 基础地址 (`dify_base_url`)**：默认 `https://api.dify.ai/v1`
- **Dify 知识库 API Key (`dify_dataset_api_key`)**：填入 `dataset-...` 密钥
- **Dify 知识库 Dataset ID (`dify_dataset_id`)**：填入**经典知识库**的 UUID
- **后台记忆总结专用模型提供商 (`summary_provider_id`)**：下拉选择已配置的模型，或留空复用当前对话模型
- **自动总结触发轮数 (`summary_turns`)**：默认 `10`
- **Dify 检索模式 (`search_method`)**：默认 `dataset_default`（强烈推荐，直接沿用你在 Dify WebUI 知识库设置中配好的混合检索、Rerank 模型与权重；若显式选择 `hybrid_search` 等其他项，则会覆盖云端检索配置且不启用云端 Rerank）
