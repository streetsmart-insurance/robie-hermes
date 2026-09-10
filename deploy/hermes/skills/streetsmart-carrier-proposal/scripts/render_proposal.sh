#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 2 ]]; then
  echo "Usage: bash render_proposal.sh data.json /absolute/output/path/proposal_name" >&2
  exit 2
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
DATA_PATH="$1"
OUTPUT_PREFIX="$2"
DOCX_PATH="${OUTPUT_PREFIX}.docx"
PDF_PATH="${OUTPUT_PREFIX}.pdf"
OUTPUT_DIR="$(dirname -- "$OUTPUT_PREFIX")"
OUTPUT_NAME="$(basename -- "$OUTPUT_PREFIX")"

if [[ ! -f "$DATA_PATH" ]]; then
  echo "Data file not found: $DATA_PATH" >&2
  exit 2
fi

if ! command -v node >/dev/null 2>&1; then
  echo "Required dependency is unavailable: node" >&2
  exit 3
fi

if command -v soffice >/dev/null 2>&1; then
  OFFICE_BIN="$(command -v soffice)"
elif command -v libreoffice >/dev/null 2>&1; then
  OFFICE_BIN="$(command -v libreoffice)"
else
  echo "LibreOffice is required to create the PDF. Do not substitute HTML or browser Print to PDF." >&2
  exit 3
fi

mkdir -p "$OUTPUT_DIR"

cd "$SCRIPT_DIR"
if ! node -e 'require("docx")' >/dev/null 2>&1; then
  if command -v pnpm >/dev/null 2>&1; then
    pnpm install --prod --no-frozen-lockfile
  elif command -v npm >/dev/null 2>&1; then
    npm install --omit=dev --save-exact
  else
    echo "Install pnpm or npm before generating the proposal." >&2
    exit 3
  fi
fi

node "$SCRIPT_DIR/generate_proposal.js" "$DATA_PATH" "$DOCX_PATH"
if [[ ! -s "$DOCX_PATH" ]]; then
  echo "DOCX generation failed: $DOCX_PATH" >&2
  exit 4
fi

RENDER_DIR="$(mktemp -d)"
cleanup() {
  rm -rf -- "$RENDER_DIR"
}
trap cleanup EXIT

LO_HOME="$RENDER_DIR/home"
LO_PROFILE="$RENDER_DIR/libreoffice-profile"
mkdir -p "$LO_HOME" "$LO_PROFILE"
HOME="$LO_HOME" "$OFFICE_BIN" --headless \
  -env:UserInstallation="file://$LO_PROFILE" \
  --convert-to pdf --outdir "$RENDER_DIR" "$DOCX_PATH" >/dev/null
RENDERED_PDF="$RENDER_DIR/${OUTPUT_NAME}.pdf"
if [[ ! -s "$RENDERED_PDF" ]]; then
  echo "PDF conversion failed: $RENDERED_PDF" >&2
  exit 4
fi
cp "$RENDERED_PDF" "$PDF_PATH"

if command -v pdfinfo >/dev/null 2>&1; then
  PAGE_COUNT="$(pdfinfo "$PDF_PATH" | awk '/^Pages:/ {print $2}')"
  PAGE_SIZE="$(pdfinfo "$PDF_PATH" | awk -F': *' '/^Page size:/ {print $2}')"
  if [[ -z "$PAGE_COUNT" || "$PAGE_COUNT" -lt 2 ]]; then
    echo "PDF integrity check failed: unexpected page count '${PAGE_COUNT:-unknown}'" >&2
    exit 5
  fi
  if [[ "$PAGE_SIZE" != *"612 x 792 pts"* ]]; then
    echo "PDF integrity check failed: expected US Letter, got '${PAGE_SIZE:-unknown}'" >&2
    exit 5
  fi
fi

echo "Created: $DOCX_PATH"
echo "Created: $PDF_PATH"
