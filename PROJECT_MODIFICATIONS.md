# Local Paperless research service changes

Upstream: https://github.com/Mountchicken/Resophy, v1.0, baseline f36572142a34f733773976d25f9b3a1117d8866d. Preserve upstream LICENSE (CC BY-NC 4.0) and attribution; its MIT classifier does not replace the license text. User confirmed personal/noncommercial research. The fork retains its independent upstream and original license.

2026-10-14–15: added research_api/store/worker/providers, a private Paperless document-bound SQLite task/artifact store, manual-result provenance, single-paper execution, saved-output reuse, explicit retry, cancellation checkpoints and interrupted-state recovery without automatic paid replay. Added owner-scoped reading data, optimistic notes, bibliography preview/confirmation, preferences, bounded Zotero candidate import and per-document research ZIP export/merge restore. Original Paperless files, permissions and classification remain authoritative.

BabelDOC 0.6.3 and its dependencies are separately pinned. Translation code saves both bilingual and Chinese PDF outputs; analysis supports a selected allowed model, custom template and output language. Provider implementations exist, but current external AI/parser access is disabled and real model output/quality/performance is unverified. Offline adapters are explicitly simulations; manual reference PDFs are not reported as translations.

The Gemini reference backend and its deployment, backup, rollback, unsafe archive extraction, virtual document IDs and conditional original-as-translation fallback were not copied. Candidate archive orchestration uses the independent Paperless gateway and only associates an actual Paperless consumer result. Local snapshot tooling lives in the root orchestration repository and restores only into a new directory.

Full F01–F30 migration and comprehensive dependency/license review remain tracked in the root project documents. New independent source files do not change or remove upstream licensing obligations. Credentials, PDFs, databases, artifacts and logs are not repository content.

2026-10-16: added content-free provider token journals, explicit real-AI opt-in, strict generated-result reuse, pinned BabelDOC CLI compatibility and protected MinerU image artifacts. Real acceptance is tracked separately; no claim of complete feature migration or commercial licensing.
