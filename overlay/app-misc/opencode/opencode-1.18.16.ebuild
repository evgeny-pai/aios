# Copyright 2026 Gentoo Authors
# Distributed under the terms of the GNU General Public License v2

EAPI=8

DESCRIPTION="The open source coding agent"
HOMEPAGE="https://opencode.ai"
# Upstream's release asset carries no version in its filename, so every release
# would collide in DISTDIR without the rename — SRC_URI's "-> ${P}.tar.gz" is
# load-bearing, not decoration.
SRC_URI="https://github.com/anomalyco/opencode/releases/download/v${PV}/opencode-linux-arm64-musl.tar.gz -> ${P}.tar.gz"

LICENSE="MIT"
SLOT="0"
# Unstable: this is the project's first forged ebuild, never reviewed as stable.
# aios.lock.json carries the matching per-package accept_keywords entry.
KEYWORDS="~arm64"

# Prebuilt for exactly one target. KEYWORDS expresses arch, not libc, so a musl
# guard belongs here — this binary would fail against glibc's dynamic linker on
# the rare host where ~arm64 alone would let it through.
pkg_pretend() {
	[[ ${CHOST} == *-musl ]] || die "${PN}: this prebuilt binary is musl-linked; ${CHOST} is not"
}

# The tarball is a bare executable at archive root — default src_prepare would
# otherwise look for ${WORKDIR}/${P} and die.
S="${WORKDIR}"

# A ~190MB single-file Bun executable carries its JS payload inside the ELF.
# portage's default prepstrip pass can corrupt it; nothing here needs stripping
# since it was never built with debug symbols this tree controls.
RESTRICT="strip"
QA_PREBUILT="usr/bin/opencode"

src_install() {
	dobin opencode
}
