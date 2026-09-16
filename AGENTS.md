# Project invariants

- The user may upload either an ordinary audio song or a supported karaoke file (`.kar`, MIDI, or `.kfn`); both are first-class product inputs.
- When the user uploads ordinary audio, the application must produce complete karaoke without requiring an additional `.kar`, MIDI, KFN, `lyricsSync.json`, stem, or other symbolic file.
- When the user explicitly uploads `.kar`, MIDI, or `.kfn`, use the existing symbolic-file pipeline and the information embedded in that uploaded file.
- Symbolic files and reference artifacts supplied only during development are test or evaluation data. They may guide universal algorithms, but must not become hidden runtime requirements or song-specific hardcoded data for audio uploads.
- Missing lyrics, notes, timing, metadata, artwork, and video must be recovered automatically from the uploaded audio, verified internet sources, and general-purpose analysis.
- Every feature or bug fix requires a dedicated failing automated test before implementation, followed by the relevant broader test suite.
- Diagnostics needed from users must be emitted automatically through the normal `start-dev.bat` console when the relevant feature runs. Do not ask users or their friends to enter separate diagnostic commands; keep automatic diagnostics bounded and non-disruptive.
