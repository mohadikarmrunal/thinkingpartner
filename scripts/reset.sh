#!/bin/zsh
# Reset ThinkingPartner. Stop the app first (Ctrl+C in its Terminal window).
#   ./scripts/reset.sh            start setup again (keeps keys, documents, downloaded models)
#   ./scripts/reset.sh --keys     also remove saved API keys from the Keychain
#   ./scripts/reset.sh --all      also remove keys and your documents
set -e
cd "$(dirname "$0")/.."

remove_keys() {
  for svc in ThinkingPartner-Anthropic ThinkingPartner-OpenAI InterviewCopilot-OpenAI; do
    security delete-generic-password -s "$svc" >/dev/null 2>&1 && echo "Removed key: $svc" || true
  done
}

rm -f settings.json .api-spend.json
echo "Settings and spend counter reset. Setup will show next time you start ThinkingPartner."
echo "Note: this creates a new iPad/phone link. Delete the old home-screen icon and pair again."

case "$1" in
  --keys) remove_keys ;;
  --all)
    remove_keys
    find context -type f ! -name README.md -delete
    echo "Removed your documents from ./context"
    ;;
esac
