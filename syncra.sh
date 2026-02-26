#!/bin/bash

# Configuration
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_MODEL="qwen3next"
LLAMA_CPP_URL="http://localhost:8080" # Default llama.cpp port

# 0. Clean up existing processes on port 9000
lsof -ti:9000 | xargs kill -9 2>/dev/null || true

# 1. Determine Model Name
MODEL="${1:-$DEFAULT_MODEL}"

# 2. Setup Environment Variables
export VLLM_URL="$LLAMA_CPP_URL"
export MODEL_NAME="$MODEL"
# Allow a list of common models for swapping
export ALLOWED_MODELS="${ALLOWED_MODELS:-$MODEL,qwen3-coder,qwen3next,devstral,falcon,deepseek-r1}"
export PORT=9000

# Tool Configuration
export ENABLE_SHELL_EXEC=true
export FILESYSTEM_ROOT="${FILESYSTEM_ROOT:-$HOME/experiments}"
export TOOL_ALLOWLIST="shell_exec"

# Create experiments dir if it doesn't exist
mkdir -p "$FILESYSTEM_ROOT"

# 3. Move to project directory
cd "$PROJECT_ROOT"

# 4. Activate Virtual Environment
if [ -f "venv/bin/activate" ]; then
    source venv/bin/activate
elif [ -f ".venv/bin/activate" ]; then
    source .venv/bin/activate
else
    echo "❌ Virtual environment not found. Please run: python3 -m venv venv && pip install -r requirements.txt"
    exit 1
fi

echo "------------------------------------------------"
echo "⚡ Syncra: Superfast Local Model Chat UI"
echo "------------------------------------------------"
echo "🤖 Model:   $MODEL"
echo "🔗 Backend: $VLLM_URL"
echo "🌐 UI:      http://localhost:$PORT"
echo "📁 Sandbox: $FILESYSTEM_ROOT"
echo "------------------------------------------------"
echo "Ready for WebSocket connections..."
echo ""

# 5. Launch Syncra
exec python -m uvicorn main:app --host 0.0.0.0 --port $PORT --log-level warning
