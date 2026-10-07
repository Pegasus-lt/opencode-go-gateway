#!/usr/bin/env python3
"""内部辅助：给 start-gateway.bat 用。检查端口上的服务是否 ogo-gw。"""
import sys
import urllib.request

try:
    with urllib.request.urlopen(sys.argv[1], timeout=4) as r:
        body = r.read().decode("utf-8", "replace")
    if "session_header" in body:
        print("OK " + body)
    else:
        print("OTHER-SERVICE " + body[:200])
except Exception as e:
    # 静默退出，交由 bat 打印提示
    sys.exit(1)