# Third-Party Notices

Jonex Platform includes modified source copies of the following runtime dependencies. Their upstream license files are preserved beside the source and apply to those directories.

This file must describe the contents that are actually distributed. If a source archive, container image, or packaged release includes a different dependency set, regenerate its third-party notices and preserve every license or NOTICE file required by the included components.

## LightRAG

- Upstream project: <https://github.com/HKUDS/LightRAG>
- Vendored version: `1.4.16` (API version `0292`)
- Local path: `Reference/LightRAG/`
- License: MIT License
- Upstream copyright: Copyright (c) 2025 LightRAG Team
- Local modifications: maintained directly in the vendored source tree and repository history

The full license text is available at `Reference/LightRAG/LICENSE`.

## RAG-Anything

- Upstream project: <https://github.com/HKUDS/RAG-Anything>
- Vendored version: `1.3.1`
- Local path: `Reference/Rag-anything/`
- License: MIT License
- Upstream copyright: Copyright (c) 2025 Data Intelligence Lab at HKU
- Local modifications: maintained directly in the vendored source tree and repository history

The full license text is available at `Reference/Rag-anything/LICENSE`.

## OpenKB

--Upstream project: <https://github.com/VectifyAI/OpenKB>
- Vendored version: `0.4.5`
- Local path: `Reference/OpenKB/
- License:   Apache 2.0
- Upstream copyright: Copyright (c) 2026  Vectify AI
- Local modifications: maintained directly in the vendored source tree and repository history

The full license text is available at `Reference/OpenKB/LICENSE`.

## Package dependencies

Python and JavaScript dependencies installed from package registries remain under their respective upstream licenses. Consult `pyproject.toml`, `uv.lock`, workspace `package.json` files, and `frontends/pnpm-lock.yaml` for the resolved dependency set.

Before publishing a source archive, container image, or binary distribution, maintainers must review the resolved dependency set, preserve required license and attribution texts, and ensure that this notice matches the shipped artifact. Downstream operators remain responsible for additional components they add to their own build profiles.
