# -*- coding: utf-8 -*-
"""腾讯云云函数（SCF）入口文件。

部署方式见 README「方案 B：腾讯云函数」。核心逻辑全在 wb_checkin.py，
这里只做一层适配：把环境变量里的凭证交给主流程，返回退出码方便看日志。
"""

import json

import wb_checkin


def main_handler(event, context):
    code = wb_checkin.main([])
    return {
        "statusCode": 200,
        "body": json.dumps({"exit_code": code}, ensure_ascii=False),
        "isBase64Encoded": False,
    }
