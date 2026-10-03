# Desktop build

This folder contains the complete, locally verified **Listii 1.0 macOS Apple Silicon build**. Requires macOS 13+ and FFmpeg for MP3 conversion. Spotify access requires your own developer app Client ID; see the root README.

The GitHub connector's transfer limit required splitting the original ZIP into eight binary parts. These parts reconstruct the exact original archive; no recompile is needed.

1. Clone this repository, or use **Code → Download ZIP** and extract the repository.
2. From the repository directory run:

   ```sh
   python3 distributions/restore.py
   ```

3. Open `distributions/Listii-macOS-arm64.zip`, extract it, and open `Listii.app`.

The restore script uses Python's standard library, verifies every part and the full archive against SHA-256 checksums, and refuses to replace a differing existing ZIP. The manifest records all sizes and checksums. The app is ad-hoc signed without Developer ID signing or notarization. Full app sources, tests, and build instructions are in the repository root. `Listii-source.zip` is an additional snapshot of the initial source package.
