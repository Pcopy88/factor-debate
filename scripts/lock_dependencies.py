"""Record the installed dependency closure for this Windows / Python 3.12 build."""
from importlib.metadata import distribution
from pathlib import Path
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

root=Path(__file__).resolve().parents[1]
pending=[Requirement(line.strip()) for line in (root/'requirements.txt').read_text().splitlines() if line.strip() and not line.startswith('#')]
visited=set()
versions={}
while pending:
    req=pending.pop()
    key=(canonicalize_name(req.name),tuple(sorted(req.extras)))
    if key in visited:continue
    visited.add(key)
    dist=distribution(req.name)
    versions[canonicalize_name(dist.metadata['Name'])]=dist.version
    for raw in dist.requires or []:
        child=Requirement(raw)
        if not child.marker or any(child.marker.evaluate({'extra':extra}) for extra in {*req.extras,''}):pending.append(child)
(root/'requirements.lock.txt').write_text('# Verified dependency snapshot: Windows, Python 3.12\n'+'\n'.join(f'{name}=={version}' for name,version in sorted(versions.items()))+'\n',encoding='utf-8')
print(f'Recorded {len(versions)} pinned packages.')
