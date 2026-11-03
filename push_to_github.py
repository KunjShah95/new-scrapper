import subprocess
import os

# Step 1: Create GitHub repo via API
print("=" * 60)
print("Step 1: Creating GitHub repository...")
print("=" * 60)

# Try to get token from git config or environment
token = os.environ.get('GITHUB_TOKEN', '')

# Build the PowerShell command carefully
ps_cmd = (
    "powershell -Command \""
    "Invoke-RestMethod -Method Post -Uri 'https://api.github.com/user/repos' "
    "-Body '{\"name\":\"new-scrapper\",\"private\":false,\"auto_init\":true}' "
    "-Headers @{'Authorization'='token " + token + "'} "
    "-ErrorAction Stop 2>$null\""
)

result = subprocess.run(ps_cmd, shell=True, capture_output=True, text=True)
print("Create repo stdout:", result.stdout[-200:])
print("Create repo stderr:", result.stderr[-200:])
print("Create repo return code:", result.returncode)

print("\n" + "=" * 60)
print("Step 2: Pushing to GitHub...")
print("=" * 60)

# Try push
result = subprocess.run(
    ['git', 'push', '-u', 'origin', 'master'],
    capture_output=True, text=True
)
print("Push stdout:", result.stdout[-300:])
print("Push stderr:", result.stderr[-300:])
print("Push return code:", result.returncode)