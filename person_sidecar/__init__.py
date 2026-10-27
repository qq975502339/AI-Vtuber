"""Repository-root import bridge for the standalone Person Sidecar package.

The installable package lives in ``person_sidecar/person_sidecar`` so it can
own its virtual environment and packaging metadata.  Extending the package
path keeps imports stable when a developer starts Python from this repository
root, where the module directory has the same name as the distribution.
"""

from pathlib import Path

__path__.append(str(Path(__file__).parent / "person_sidecar"))
