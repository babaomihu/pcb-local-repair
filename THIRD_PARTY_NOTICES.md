# Third-party notices and license scope

This notice applies to the minimal **0.9.5.dev13** source distribution prepared on 2026-10-30. Upstream ownership and per-file licenses remain unchanged.

## Project code

Copyright (c) 2026 PCB Local Repair contributors.

Project-authored integration code in `src/pcb_local/`, `scripts/`, the KiCad launcher, and test programs is licensed under GNU GPL version 3 or, at your option, any later version (`GPL-3.0-or-later`), except for the KRT patch contribution exception and fixture dedication below. Project-authored documentation and configuration use the same license unless stated otherwise. See `LICENSE`.

## KiCadRoutingTools and MIT contribution exception

- Source: https://github.com/drandyhaas/KiCadRoutingTools
- Pinned base: `bda59fa09496648d4677d8dc17b2ab60af6a49b1`, version 0.23.7.
- Upstream copyright: Copyright (c) 2026 drandyhaas.
- License: MIT; original text in `licenses/KiCadRoutingTools-MIT.txt`.
- KRT is fetched/built separately; it is not relicensed by the integration package.

All project-authored changes and new tests supplied in `patches/krt-*.patch` are offered under MIT. Copyright (c) 2026 PCB Local Repair contributors applies to those contributions. Preserve both the upstream and contribution notices when redistributing the patches or patched source.

The MIT exception includes generated-teardrop anchor handling, native-compatible netclass resolution, literal net selection, endpoint/local-search adaptation, bounded restoration, and resource-labeled search. Exact files and patch identities are recorded in the bundled KRT manifests. Historical patch sets remain because the build catalog verifies their bytes; the current default is `v0.8.9`.

The chamfered-pad parser backport derives from upstream commit `b0d9326d1d49830b7e2124ec122b8d4b8d6ab431`, also MIT. Patch context from existing upstream files remains upstream-authored code. These contributions do not claim authorship of KRT's original routing engine.

## PCBWorld-Engine, kicad-python and KiCad PNS

- PCBWorld-Engine: https://github.com/LGAI-Research/PCBWorld-Engine
- Engine pin: `30d6a68261b8631e58f0a39c915b1cd9467bf00a`.
- kicad-python pin: `66ff9ec17d247153aab9e4b7bee1fdbd3ef199ff`.
- Internal KiCad pin: `b09c98053a892ff12d5c4f3c2070ca9bfca20106` (9.0.8).
- Engine license text: `licenses/PCBWorld-Engine-GPL-3.0.txt`.
- KiCad upstream license overview: `licenses/KiCad-LICENSE.README.upstream.txt`.

This is an optional separately built process dependency. The package contains our adapters and source/build recipes, not compiled engine binaries or the PCBWorld training environment. The PNS shove, checkpoint and restore capabilities are upstream work; the integration does not claim a newly authored shove kernel. Individual upstream source files retain their own notices.

## KiCad 10 native validation patch

- Source: https://github.com/KiCad/kicad-source-mirror
- Pinned commit: `f7414d419cae5df2d00e7eaacb16fc0e803799bc` (10.0.4).
- Patch: `patches/kicad10-drc-report-capacity.patch`.
- Original notices: `licenses/KiCad-10.0.4-LICENSE.README.upstream.txt` and `licenses/KiCad-10.0.4-AUTHORS.upstream.txt`.

The patch preserves upstream context and notices. Project changes increase report capacity, expose coverage diagnostics and fix build integration; the combined distributed patch is GPL-3.0-or-later. KiCad applications, native libraries and compiled outputs are not bundled here.

## Geometry and Python dependencies

Python, Rust, NumPy, SciPy, Shapely/GEOS, Pillow, pybind11, build tools and transitive crates/libraries retain their own licenses. They are external dependencies, not included binaries. See `requirements-krt.lock.txt`, `requirements-cli-build.lock.txt`, `runtime-lock.json`, and the fetched KRT `Cargo.lock` for pins.

Our Python geometry/strategy code uses established techniques such as configuration-space obstacles, visibility graphs, A*, and bounded candidate coordination. Algorithm review of tscircuit/tscircuit-autorouter and J-Kreisel/kicad-autorouter informed design; no source from those repositories is copied into this runtime. Freerouting and other isolated research engines are not included in this distribution.

## Authored mechanism fixtures

The independently authored geometry generators `tests/geometry_fixture.py` and `tests/legacy_fixture.py`, and generated fixture geometry under `examples/local-middle-native/`, are dedicated under **CC0-1.0**, as identified in their source notices. See https://creativecommons.org/publicdomain/zero/1.0/ for the dedication. Other test programs remain GPL-3.0-or-later.

The local-middle example is an artificial protection test: one manually constructed candidate and one tampered negative control. It is not an original hardware project or evidence of an automatically solved real-world defect.

No third-party hardware project, user board, dataset, runtime configuration, virtual environment, compiled module, or historical board-derived geometry is included in this minimal repository. The archived Royal/Tiny/motor and other engineering boards are outside this distribution.

## Redistribution

Keep this notice, applicable original notices, and license texts with redistributed source. A future binary/virtual-environment bundle needs its own dependency-license and corresponding-source review; these source-only notices do not supply all materials for such a binary distribution.
