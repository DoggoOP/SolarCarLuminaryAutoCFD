#!/bin/bash

# Script to prepare Google credentials for deployment
# This converts the JSON file to a single-line string suitable for environment variables

echo "=========================================="
echo "Preparing credentials for deployment"
echo "=========================================="
echo ""

# Check if jq is installed
if ! command -v jq &> /dev/null; then
    echo "Error: jq is not installed."
    echo "Install it with: brew install jq"
    exit 1
fi

# Find the credentials file
CREDS_FILE="SolarCarLuminaryBotIAMAdmin.json"

if [ ! -f "$CREDS_FILE" ]; then
    echo "Error: $CREDS_FILE not found!"
    echo "Make sure you're in the project root directory."
    exit 1
fi

echo "Found credentials file: $CREDS_FILE"
echo ""
echo "Converting to single-line JSON..."
echo ""

# Convert to single line
SINGLE_LINE=$(cat "$CREDS_FILE" | jq -c)

echo "=========================================="
echo "Copy this value for GOOGLE_SHEETS_CREDENTIALS:"
echo "=========================================="
echo "$SINGLE_LINE"
echo ""
echo "=========================================="
echo "Done!"
echo "=========================================="
echo ""
echo "Next steps:"
echo "1. Copy the JSON string above"
echo "2. In your deployment platform (Railway/Render/etc.):"
echo "   - Go to Environment Variables"
echo "   - Create a new variable: GOOGLE_SHEETS_CREDENTIALS"
echo "   - Paste the entire JSON string as the value"
echo "3. Make sure to also set:"
echo "   - LUMINARY_API_KEY"
echo "   - GOOGLE_SHEETS_SPREADSHEET_ID"
echo "   - LUMINARY_PROJECT_NAME"
echo ""
