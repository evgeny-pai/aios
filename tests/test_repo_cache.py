"""Which atoms this node can restore from its own binary package cache.

The function exists because of a specific failure: a boot-time restore spelled
`emerge --usepkgonly @aios` aborted in dependency resolution on the first atom with no
binary package and installed none of the ones it had. The parsing is the fiddly part —
a package name may contain hyphens, so the version boundary is not "the last hyphen".
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from aios import repo as repo_mod

INDEX = """\
PKGDIR: /var/cache/binpkgs

CPV: sys-devel/distcc-3.4-r9
BUILD_ID: 1

CPV: app-text/docbook-xml-dtd-4.1.2-r7
BUILD_ID: 1

CPV: media-libs/libjpeg-turbo-3.1.4.1
BUILD_ID: 1

CPV: dev-util/ccache-4.13.5
BUILD_ID: 1

CPV: sys-libs/binutils-libs-2.46.0
BUILD_ID: 2
"""


class TestCachedAtoms(unittest.TestCase):
    def index(self, text: str) -> Path:
        path = Path(tempfile.mkdtemp()) / "Packages"
        path.write_text(text, encoding="utf-8")
        return path

    def test_hyphenated_names_survive_version_stripping(self):
        """The whole point: splitting on the last hyphen would give `docbook-xml`."""
        atoms = repo_mod.cached_atoms(self.index(INDEX))
        self.assertIn("app-text/docbook-xml-dtd", atoms)
        self.assertIn("media-libs/libjpeg-turbo", atoms)
        self.assertIn("sys-libs/binutils-libs", atoms)
        self.assertIn("sys-devel/distcc", atoms)
        self.assertIn("dev-util/ccache", atoms)

    def test_no_version_or_revision_leaks_through(self):
        for atom in repo_mod.cached_atoms(self.index(INDEX)):
            self.assertNotRegex(atom, r"-[0-9]", f"{atom} still carries a version")
            self.assertEqual(atom.count("/"), 1)

    def test_multiple_build_ids_collapse_to_one_atom(self):
        """binpkg-multi-instance keeps several builds of one package; emerge wants one."""
        doubled = INDEX + "\nCPV: sys-devel/distcc-3.4-r9\nBUILD_ID: 2\n"
        atoms = repo_mod.cached_atoms(self.index(doubled))
        self.assertEqual(atoms.count("sys-devel/distcc"), 1)

    def test_the_result_is_sorted_and_deduplicated(self):
        atoms = repo_mod.cached_atoms(self.index(INDEX))
        self.assertEqual(atoms, sorted(set(atoms)))

    def test_a_missing_index_is_an_empty_list_not_a_crash(self):
        """A first boot has no cache, and that is not an error — it is a source build."""
        self.assertEqual(repo_mod.cached_atoms(Path("/nonexistent/Packages")), [])

    def test_a_junk_index_yields_nothing_rather_than_junk_atoms(self):
        atoms = repo_mod.cached_atoms(self.index("garbage\nCPV:\nCPV: noslash-1.0\n"))
        self.assertEqual(atoms, [])


if __name__ == "__main__":
    unittest.main()
