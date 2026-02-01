import json
import os

def load_file(filename):
    if os.path.exists(filename):
        with open(filename, 'r') as f:
            content = f.read().strip()
            # Verify it's valid JSON
            try:
                json.loads(content)
                return content
            except json.JSONDecodeError:
                print(f"Error: {filename} is not valid JSON.")
                return None
    else:
        print(f"Error: {filename} not found.")
        return None

print("--- GENERATING ENV VARS FOR RENDER ---")
print("1. Go to your Render Dashboard -> Service -> Environment")
print("2. Add the following Environment Variables:")

token = load_file('token.json')
if token:
    print("\nKey: GOOGLE_TOKEN_JSON")
    print("Value:")
    print(token)
else:
    print("\n[WARNING] token.json missing! Authenticate locally first.")

creds = load_file('credentials.json')
if creds:
    print("\nKey: GOOGLE_CREDENTIALS_JSON")
    print("Value:")
    print(creds)
else:
    print("\n[WARNING] credentials.json missing!")

print("\n--------------------------------------")
