#!/usr/bin/env bash
# 重新生成 src/simulator/realtime/proto/ 下的 _pb2*.py。
# 生成产物提交入库；仅 schema 变更时需要重跑本脚本（需要 dev extra）。
set -euo pipefail
cd "$(dirname "$0")/.."
uv run --extra dev python -m grpc_tools.protoc \
  -I src \
  --python_out=src \
  --grpc_python_out=src \
  --pyi_out=src \
  src/simulator/realtime/proto/realtime_env.proto
echo "generated: src/simulator/realtime/proto/realtime_env_pb2*.py"
