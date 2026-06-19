#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
# shellcheck source=scripts/lib/python-bin.sh
source "$ROOT/scripts/lib/python-bin.sh"
PYTHON_BIN="$(resolve_python_bin "$ROOT")"
export PYTHON_BIN

ROLE=""
MODEL_ID="Qwen/Qwen3-0.6B"
AS_OF_DATE=""
CONFIRM_TRAIN=0
CONFIRM_DOWNLOAD=0
EPOCHS="1"
LEARNING_RATE="0.0002"
BATCH_SIZE="1"
GRADIENT_ACCUMULATION_STEPS="1"
MAX_STEPS="-1"
LORA_RANK="8"
LORA_ALPHA="16"
LORA_DROPOUT="0.05"
DTYPE="auto"
DEVICE="auto"

usage() {
  cat <<'USAGE'
usage: scripts/run-llm-local-training.sh --role ROLE --model-id MODEL --as-of-date YYYY-MM-DD --confirm-train [--confirm-download]

Runs the local-only LLM training workflow:
dataset -> deterministic supervision -> TRL export -> cache verify -> LoRA SFT
-> adapter smoke -> eval -> adapter report.

Install local training dependencies first with:
  python -m pip install -e ".[local-llm]"

Model downloads are blocked unless --confirm-download is provided.
USAGE
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --role)
      ROLE="${2:-}"
      shift 2
      ;;
    --model-id)
      MODEL_ID="${2:-}"
      shift 2
      ;;
    --as-of-date)
      AS_OF_DATE="${2:-}"
      shift 2
      ;;
    --confirm-train)
      CONFIRM_TRAIN=1
      shift
      ;;
    --confirm-download)
      CONFIRM_DOWNLOAD=1
      shift
      ;;
    --epochs)
      EPOCHS="${2:-}"
      shift 2
      ;;
    --learning-rate)
      LEARNING_RATE="${2:-}"
      shift 2
      ;;
    --batch-size)
      BATCH_SIZE="${2:-}"
      shift 2
      ;;
    --gradient-accumulation-steps)
      GRADIENT_ACCUMULATION_STEPS="${2:-}"
      shift 2
      ;;
    --max-steps)
      MAX_STEPS="${2:-}"
      shift 2
      ;;
    --lora-rank)
      LORA_RANK="${2:-}"
      shift 2
      ;;
    --lora-alpha)
      LORA_ALPHA="${2:-}"
      shift 2
      ;;
    --lora-dropout)
      LORA_DROPOUT="${2:-}"
      shift 2
      ;;
    --dtype)
      DTYPE="${2:-}"
      shift 2
      ;;
    --device)
      DEVICE="${2:-}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [ -z "$ROLE" ] || [ -z "$MODEL_ID" ] || [ -z "$AS_OF_DATE" ]; then
  echo "--role, --model-id, and --as-of-date are required" >&2
  usage >&2
  exit 2
fi

if [[ ! "$AS_OF_DATE" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]]; then
  echo "--as-of-date must be YYYY-MM-DD" >&2
  exit 2
fi

if [ "$CONFIRM_TRAIN" -ne 1 ]; then
  echo "local training requires --confirm-train" >&2
  exit 2
fi

cd "$ROOT"
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH="${PYTHONPATH:-src}"

REGISTRY="${LLM_LOCAL_REGISTRY:-configs/llm_local_models.json}"
CACHE_ROOT="${LLM_LOCAL_CACHE_ROOT:-models/local/weights}"
SOURCE_ROOT="${LLM_LOCAL_SOURCE_ROOT:-reports/tmp}"
OUTPUT_ROOT="${LLM_LOCAL_TRAINING_ROOT:-reports/tmp/llm_local_training/$ROLE/$AS_OF_DATE}"
DATASET_DIR="$OUTPUT_ROOT/dataset"
SUPERVISION_DIR="$OUTPUT_ROOT/supervision"
EXPORT_DIR="$OUTPUT_ROOT/export"
SFT_DIR="$OUTPUT_ROOT/sft"
ADAPTER_DIR="$OUTPUT_ROOT/adapter"
SMOKE_DIR="$OUTPUT_ROOT/smoke"
EVAL_DIR="$OUTPUT_ROOT/eval"
ADAPTER_REPORT_DIR="$OUTPUT_ROOT/adapter_report"
CACHE_REPORT="$OUTPUT_ROOT/cache_verify.json"

mkdir -p "$OUTPUT_ROOT"

run_step() {
  local name="$1"
  shift

  printf '\n==> %s\n' "$name"
  "$@"
}

download_model() {
  "$PYTHON_BIN" - "$MODEL_ID" "$REGISTRY" "$CACHE_ROOT" <<'PY'
from __future__ import annotations

import json
import sys
from pathlib import Path

model_id, registry_path, cache_root = sys.argv[1:4]
registry = json.loads(Path(registry_path).read_text(encoding="utf-8"))
entry = next(
    (item for item in registry.get("models", []) if isinstance(item, dict) and item.get("model_id") == model_id),
    None,
)
local_dir = str((entry or {}).get("local_dir") or model_id.replace("/", "-").lower())
target = Path(cache_root) / local_dir
target.parent.mkdir(parents=True, exist_ok=True)
try:
    from huggingface_hub import snapshot_download
except ModuleNotFoundError as exc:
    raise SystemExit(
        "huggingface_hub is required for --confirm-download. "
        'Install local LLM dependencies with: python -m pip install -e ".[local-llm]" '
        f"({exc})"
    )
snapshot_download(
    repo_id=model_id,
    local_dir=str(target),
    local_dir_use_symlinks=False,
    resume_download=True,
)
print(f"downloaded {model_id} into {target}")
PY
}

if ! "$PYTHON_BIN" -m trading_ai.cli llm-local-cache-verify \
  --model-id "$MODEL_ID" \
  --registry "$REGISTRY" \
  --cache-root "$CACHE_ROOT" \
  --output "$CACHE_REPORT"; then
  if [ "$CONFIRM_DOWNLOAD" -ne 1 ]; then
    echo "local model cache missing for $MODEL_ID; rerun with --confirm-download to fetch weights explicitly" >&2
    exit 2
  fi
  run_step "download model cache" download_model
  run_step "verify downloaded model cache" "$PYTHON_BIN" -m trading_ai.cli llm-local-cache-verify \
    --model-id "$MODEL_ID" \
    --registry "$REGISTRY" \
    --cache-root "$CACHE_ROOT" \
    --output "$CACHE_REPORT"
fi

run_step "build LLM training dataset" "$PYTHON_BIN" -m trading_ai.cli llm-training-dataset \
  --role "$ROLE" \
  --as-of-date "$AS_OF_DATE" \
  --source-root "$SOURCE_ROOT" \
  --output-dir "$DATASET_DIR"

DATASET_JSON="$DATASET_DIR/$ROLE/$AS_OF_DATE/dataset.json"
HOLDOUT_JSONL="$DATASET_DIR/$ROLE/$AS_OF_DATE/holdout.jsonl"

run_step "supervise labels locally" "$PYTHON_BIN" -m trading_ai.cli llm-supervise-labels \
  --role "$ROLE" \
  --dataset "$DATASET_JSON" \
  --frontier-model "$MODEL_ID" \
  --output-dir "$SUPERVISION_DIR"

LABELS_JSON="$SUPERVISION_DIR/$ROLE/labels.json"

run_step "export TRL training JSONL" "$PYTHON_BIN" -m trading_ai.cli llm-training-export \
  --role "$ROLE" \
  --supervised-dataset "$LABELS_JSON" \
  --format trl-jsonl \
  --output-dir "$EXPORT_DIR"

TRAINING_JSONL="$EXPORT_DIR/$ROLE/training.jsonl"
SFT_MANIFEST="$SFT_DIR/manifest.json"

run_step "run local LoRA SFT" "$PYTHON_BIN" -m trading_ai.cli llm-local-sft \
  --role "$ROLE" \
  --base-model-id "$MODEL_ID" \
  --training-jsonl "$TRAINING_JSONL" \
  --adapter-dir "$ADAPTER_DIR" \
  --registry "$REGISTRY" \
  --cache-root "$CACHE_ROOT" \
  --epochs "$EPOCHS" \
  --learning-rate "$LEARNING_RATE" \
  --batch-size "$BATCH_SIZE" \
  --gradient-accumulation-steps "$GRADIENT_ACCUMULATION_STEPS" \
  --max-steps "$MAX_STEPS" \
  --lora-rank "$LORA_RANK" \
  --lora-alpha "$LORA_ALPHA" \
  --lora-dropout "$LORA_DROPOUT" \
  --dtype "$DTYPE" \
  --device "$DEVICE" \
  --output "$SFT_MANIFEST"

SMOKE_JSON="$SMOKE_DIR/smoke.json"
run_step "smoke adapter inference" "$PYTHON_BIN" -m trading_ai.cli llm-local-smoke \
  --model-id "$MODEL_ID" \
  --registry "$REGISTRY" \
  --cache-root "$CACHE_ROOT" \
  --adapter-manifest "$SFT_MANIFEST" \
  --output "$SMOKE_JSON"

run_step "evaluate local adapter candidate" "$PYTHON_BIN" -m trading_ai.cli llm-local-eval-suite \
  --role "$ROLE" \
  --candidate "$LABELS_JSON" \
  --holdout "$HOLDOUT_JSONL" \
  --base-model-id "$MODEL_ID" \
  --adapter-manifest "$SFT_MANIFEST" \
  --output-dir "$EVAL_DIR"

EVAL_REPORT="$EVAL_DIR/$ROLE/eval_report.json"
run_step "write adapter report" "$PYTHON_BIN" -m trading_ai.cli llm-local-adapter-report \
  --role "$ROLE" \
  --sft-manifest "$SFT_MANIFEST" \
  --eval-report "$EVAL_REPORT" \
  --smoke-report "$SMOKE_JSON" \
  --output-dir "$ADAPTER_REPORT_DIR"

printf '\nlocal LLM training artifacts: %s\n' "$OUTPUT_ROOT"
