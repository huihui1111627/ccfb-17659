# -*- coding: utf-8 -*-
"""Web 服务启动入口：python -m icconv.web.run --path ./demo_data"""
from __future__ import annotations

import argparse

from .app import create_app


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--path", default="./demo_data")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5000)
    args = ap.parse_args(argv)
    app = create_app(args.path)
    print(f"隐式转换分析平台启动：http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=False)


if __name__ == "__main__":
    main()
