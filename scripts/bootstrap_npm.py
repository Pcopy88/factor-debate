"""Install a project-local npm without changing the system Node installation."""
from pathlib import Path
import tarfile
import urllib.request

root = Path(__file__).resolve().parents[1] / ".tools"
root.mkdir(exist_ok=True)
archive = root / "npm.tgz"
urllib.request.urlretrieve("https://registry.npmjs.org/npm/-/npm-10.9.4.tgz", archive)
with tarfile.open(archive) as tar:
    tar.extractall(root / "npm", filter="data")
print(root / "npm/package/bin/npm-cli.js")
