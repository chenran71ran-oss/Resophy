# Research integration sources and licenses

- Upstream Resophy: https://github.com/Mountchicken/Resophy at
  f36572142a34f733773976d25f9b3a1117d8866d. Its LICENSE is **CC BY-NC 4.0**.
  The MIT classifier in upstream metadata does not replace that license.
- BabelDOC: https://github.com/funstory-ai/BabelDOC, installed package **0.6.3**.
  Wheel metadata reports `AGPL-3.0`; its unmodified license is included in
  `licenses/BabelDOC-0.6.3-LICENSE`. The separately executed
  `resophy/babeldoc_runner.py` adapter subclasses its translator and is provided
  under GNU AGPL v3. Its modifications disable automatic retries and record
  content-free provider usage. Keep this source and its notices available with
  the worker image and any hosted modified version.
- The new independent `resophy/research_usage.py` utility is explicitly MIT,
  under `licenses/research-usage-MIT.txt`. This permits its use from both the
  standalone BabelDOC adapter and the research service. This exception does
  not relicense any upstream Resophy code or other files.
- Worker dependencies retain their own notices, including PyMuPDF and
  BabelDOC's PDFMiner fork. Pinned versions are in
  `requirements-research-ai.lock`; installed distribution license files are
  retained. The core research service communicates with the BabelDOC adapter
  through a subprocess, not an embedded model service.
- Paperless remains an independent GPL-3.0-only project communicating through
  the protected HTTP gateway. Its license and third-party UI notices remain in
  its own repository/image.

This source-available integration retains the noncommercial Resophy restriction;
it is **not** a blanket MIT/GPL relicense, an assertion that the entire suite is
OSI open source, or a complete transitive license/security certification.
Commercial redistribution requires resolving the original restrictions.
