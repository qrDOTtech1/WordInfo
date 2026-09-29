# SAF Engine — Windows preview

## What this build contains
Native desktop foundation only. Five informational tabs and minimal local exception reports. No engine integration, orders, AI-provider discovery, automatic GitHub report upload or updater. No dependency on a browser, Docker or an installed Python interpreter for the packaged application.

## Build status
The workflow must finish successfully before an executable is considered available. A GUI smoke test runs offscreen against Python sources; it does not validate the packaged executable on a clean Windows machine. This preview uses PyInstaller onedir, not onefile, to simplify diagnosis. Dependencies and bundling need further Windows validation. No executable signing is configured.

## Download after a successful workflow
Open the repository Actions tab, select SAF desktop Windows preview, and open a successful run. Download SAF-Engine-Windows-Preview. Extract the artifact, then extract SAF_Engine_Windows_Preview.zip. Keep the entire SAF_Engine folder including _internal. Launch SAF_Engine.exe inside that folder. Do not copy just the executable.

The SHA256 file identifies the build archive; it is not a digital signature or proof of publisher identity. Only use an artifact from a trusted repository run. Do not disable Windows security protections to launch an unverified build.

## Local source testing
Use the development environment described in SAF_DESKTOP_PLAN.md, then run:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_saf_gui.py -v
```

The test uses Qt's offscreen platform. Actual visual, scaling, accessibility and packaged-application tests remain necessary on Windows.
