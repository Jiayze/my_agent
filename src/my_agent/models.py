"""智能体框架的内部领域类型。
随着后续开发阶段的推进，AgentResult、AgentRunContext 以及其他共享的数据结构都将存放于此。属于领域层的错误类型也保存在这个文件中，这样其他模块在导入它们时，就不会产生循环导入.
"""


class ModelConfigurationError(ValueError):
    """当模型配置无效或客户端创建失败时抛出"""
