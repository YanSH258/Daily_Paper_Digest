"""
integrations - 外部服务集成（Zotero / Web of Science / OpenAlex）

约定：
- 所有 API key 通过环境变量或 config.yaml 提供，绝不写入代码仓库
- 网络异常一律捕获并返回明确错误，不阻断主流程
"""
