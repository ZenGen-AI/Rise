"""组合根的装配错误。命令行把它打印成一行错误并以退出码 2 退出,不静默降级。"""
from __future__ import annotations


class FactoryConfigError(Exception):
    """装配所需的配置、凭证或资产缺失或不合法。信息里只有路径、键名、变量名,绝不含凭证值。"""
