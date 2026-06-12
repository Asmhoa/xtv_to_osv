#!/bin/bash
set -euo pipefail

app_path="${1:-dist/XtraToOsmo.app}"
keychain_path="$RUNNER_TEMP/xtra-to-osmo-signing.keychain-db"
certificate_path="$RUNNER_TEMP/xtra-to-osmo-signing.p12"
notary_archive="$RUNNER_TEMP/xtra-to-osmo-notary.zip"
keychain_password="xtra-to-osmo-${GITHUB_RUN_ID:-local}"

CERTIFICATE_PATH="$certificate_path" \
  python3 -c 'import base64, os, pathlib; pathlib.Path(os.environ["CERTIFICATE_PATH"]).write_bytes(base64.b64decode(os.environ["MACOS_CERTIFICATE"]))'

security create-keychain -p "$keychain_password" "$keychain_path"
security set-keychain-settings -lut 21600 "$keychain_path"
security unlock-keychain -p "$keychain_password" "$keychain_path"
security import "$certificate_path" \
  -k "$keychain_path" \
  -P "$MACOS_CERTIFICATE_PASSWORD" \
  -T /usr/bin/codesign
security set-key-partition-list \
  -S apple-tool:,apple:,codesign: \
  -s \
  -k "$keychain_password" \
  "$keychain_path"

codesign \
  --deep \
  --force \
  --options runtime \
  --timestamp \
  --sign "$MACOS_SIGNING_IDENTITY" \
  "$app_path"
codesign --verify --deep --strict --verbose=2 "$app_path"

ditto -c -k --keepParent "$app_path" "$notary_archive"
xcrun notarytool submit "$notary_archive" \
  --apple-id "$APPLE_ID" \
  --team-id "$APPLE_TEAM_ID" \
  --password "$APPLE_APP_PASSWORD" \
  --wait
xcrun stapler staple "$app_path"

security delete-keychain "$keychain_path"
rm -f "$certificate_path" "$notary_archive"
